"""
Módulo de COMPRAS a proveedores: es el mecanismo de reposición de stock.
Al confirmar una compra, se inserta la Compra + su Detalle_Compra, y por
cada línea se incrementa Producto.Stock automáticamente (todo en una sola
transacción: si algo falla, no se descuadra el stock).

Quién entra lo decide la opción de menú asignada a cada perfil en
"Asignar Accesos" (acceso_requerido). Una compra confirmada no se edita ni se borra (regla de negocio: es un
comprobante), solo se puede consultar su detalle.
"""
import json
import re
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from flask import render_template, request, redirect, url_for, flash, session, current_app
from db import conexion_actual, resultado_procedimiento
from paginacion import consultar_pagina
from routes.administrador import admin_bp, acceso_requerido

# IGV Perú (18%). Decimal, no float: 0.18 en float no es exacto y el redondeo
# del impuesto podría salir un céntimo distinto.
IGV = Decimal('0.18')
CENTIMO = Decimal('0.01')
MONTO_MAXIMO = Decimal('99999999.99')  # columnas NUMERIC(10,2) de Compra y Detalle_Compra


@admin_bp.route('/compras')
@acceso_requerido
def lista_compras():
    compras = consultar_pagina(
        """
        SELECT c.IdCompra, c.Fecha, pr.RazonSocial, c.SubTotal, c.IGV, c.Total,
               u.Nombres, u.ApellidoPaterno
        FROM Compra c
        INNER JOIN Proveedor pr ON pr.IdProveedor = c.IdProveedor
        LEFT JOIN Usuario u ON u.IdUsuario = c.IdUsuario
        """,
        orden='c.Fecha DESC, c.IdCompra DESC',
    )
    return render_template('admin/compras.html', compras=compras)


@admin_bp.route('/compras/nueva', methods=['GET', 'POST'])
@acceso_requerido
def nueva_compra():
    proveedores, productos = _obtener_proveedores_y_productos_activos()

    if request.method == 'POST':
        id_proveedor = request.form.get('id_proveedor', '').strip()
        ids_producto = request.form.getlist('producto_id[]')
        cantidades = request.form.getlist('cantidad[]')
        precios = request.form.getlist('precio[]')
        # Lo enviado, para volver a mostrar el formulario tal cual si hay un error.
        enviado = {'id_proveedor': id_proveedor,
                   'lineas': [{'producto_id': i, 'cantidad': c, 'precio': p}
                              for i, c, p in zip(ids_producto, cantidades, precios)]}

        lineas, error = _leer_lineas_compra(id_proveedor, ids_producto, cantidades, precios, proveedores, productos)
        if error:
            flash(error, 'error')
            return render_template('admin/compra_form.html', proveedores=proveedores, productos=productos, enviado=enviado)

        subtotal, igv, total = calcular_totales(lineas)
        if total > MONTO_MAXIMO:
            flash(f'El total de la compra (S/ {total}) supera el máximo permitido (S/ {MONTO_MAXIMO}).', 'error')
            return render_template('admin/compra_form.html', proveedores=proveedores, productos=productos, enviado=enviado)

        # HU-11: procedimiento usp_Compra_Registrar (sql/stored_procedures.sql). En UNA
        # transacción registra la Compra, sus Detalle_Compra y repone el stock; calcula
        # SubTotal, IGV y Total con la misma regla que calcular_totales(). Si un producto
        # fue desactivado mientras se llenaba el formulario, o cualquier paso falla, no
        # graba nada. Las líneas viajan en JSON (precios como texto: exactos, sin float).
        detalle = json.dumps([{'IdProducto': l['id_producto'], 'Cantidad': l['cantidad'], 'Precio': str(l['precio'])}
                              for l in lineas])
        conexion = conexion_actual()
        cursor = conexion.cursor()
        try:
            cursor.execute(
                "EXEC dbo.usp_Compra_Registrar @IdProveedor = ?, @IdUsuario = ?, @Fecha = ?, @Detalle = ?",
                int(id_proveedor), session['id_usuario'], datetime.now(), detalle,
            )
            resultado = resultado_procedimiento(cursor)
            if resultado.Resultado != 0:
                conexion.rollback()
                flash('No se pudo registrar la compra: uno de los productos ya no está activo; '
                      'no se guardó ningún cambio.', 'error')
                return render_template('admin/compra_form.html', proveedores=proveedores, productos=productos, enviado=enviado)

            conexion.commit()
            id_compra = resultado.IdCompra
            flash(f'Compra #{id_compra} registrada. Stock actualizado correctamente.', 'success')
            return redirect(url_for('admin.ver_compra', id_compra=id_compra))
        except Exception:
            conexion.rollback()
            # El detalle técnico va al log del servidor, nunca a la pantalla.
            current_app.logger.exception('Error al registrar compra del proveedor %s', id_proveedor)
            flash('No se pudo registrar la compra; no se guardó ningún cambio. Revisa los datos e inténtalo nuevamente.', 'error')
            return render_template('admin/compra_form.html', proveedores=proveedores, productos=productos, enviado=enviado)

    return render_template('admin/compra_form.html', proveedores=proveedores, productos=productos, enviado=None)


def calcular_totales(lineas):
    """
    HU-11: Subtotal = suma de (cantidad x precio); IGV = 18% del subtotal, redondeado
    al céntimo (mitad hacia arriba); Total = Subtotal + IGV.
    """
    subtotal = sum((l['cantidad'] * l['precio'] for l in lineas), Decimal('0')).quantize(CENTIMO)
    igv = (subtotal * IGV).quantize(CENTIMO, rounding=ROUND_HALF_UP)
    return subtotal, igv, subtotal + igv


@admin_bp.route('/compras/<int:id_compra>')
@acceso_requerido
def ver_compra(id_compra):
    conexion = conexion_actual()
    cursor = conexion.cursor()
    cursor.execute(
        """
        SELECT c.IdCompra, c.Fecha, pr.RazonSocial, pr.NumDocIdentidad,
               c.SubTotal, c.IGV, c.Total, u.Nombres, u.ApellidoPaterno
        FROM Compra c
        INNER JOIN Proveedor pr ON pr.IdProveedor = c.IdProveedor
        LEFT JOIN Usuario u ON u.IdUsuario = c.IdUsuario
        WHERE c.IdCompra = ?
        """,
        id_compra,
    )
    compra = cursor.fetchone()

    cursor.execute(
        """
        SELECT p.Nombre, dc.Cantidad, dc.Precio, (dc.Cantidad * dc.Precio) AS Importe
        FROM Detalle_Compra dc
        INNER JOIN Producto p ON p.IdProducto = dc.IdProducto
        WHERE dc.IdCompra = ?
        """,
        id_compra,
    )
    detalle = cursor.fetchall()

    if compra is None:
        flash('Esa compra no existe.', 'error')
        return redirect(url_for('admin.lista_compras'))

    return render_template('admin/compra_detalle.html', compra=compra, detalle=detalle)


def _obtener_proveedores_y_productos_activos():
    conexion = conexion_actual()
    cursor = conexion.cursor()
    cursor.execute("SELECT IdProveedor, RazonSocial FROM Proveedor WHERE Activo = 1 ORDER BY RazonSocial")
    proveedores = cursor.fetchall()
    cursor.execute("SELECT IdProducto, Nombre, Precio, Stock FROM Producto WHERE Activo = 1 ORDER BY Nombre")
    productos = cursor.fetchall()
    return proveedores, productos


def _leer_lineas_compra(id_proveedor, ids_producto, cantidades, precios, proveedores, productos):
    """
    Valida y arma la lista de líneas de la compra. Devuelve (lineas, mensaje_error).
    El HTML ya restringe estos datos, pero se vuelven a validar aquí porque el
    navegador se puede saltar (formulario editado a mano, petición directa, etc.).
    """
    if not id_proveedor.isdigit() or int(id_proveedor) not in {p.IdProveedor for p in proveedores}:
        return None, 'Selecciona un proveedor válido (activo).'

    if not ids_producto:
        return None, 'Agrega al menos un producto a la compra.'
    if not len(ids_producto) == len(cantidades) == len(precios):
        return None, 'El detalle de la compra está incompleto. Vuelve a cargar el formulario.'

    ids_activos = {p.IdProducto for p in productos}
    lineas = []
    for n, (id_producto, cantidad, precio) in enumerate(zip(ids_producto, cantidades, precios), start=1):
        id_producto, cantidad, precio = id_producto.strip(), cantidad.strip(), precio.strip()
        if not id_producto.isdigit() or int(id_producto) not in ids_activos:
            return None, f'Línea {n}: selecciona un producto válido (activo).'
        # Cantidad: entero de 1 en adelante (columna INT).
        if not re.fullmatch(r'\d{1,9}', cantidad) or int(cantidad) == 0:
            return None, f'Línea {n}: la cantidad debe ser un número entero mayor que 0.'
        # Precio: mayor que 0, hasta 2 decimales (columna DECIMAL(10,2)).
        # (float() aceptaría "nan", "inf", "-5" o "1e3"; por eso se valida el texto exacto.)
        if not re.fullmatch(r'\d{1,8}(\.\d{1,2})?', precio) or Decimal(precio) <= 0:
            return None, f'Línea {n}: el precio debe ser mayor que 0, con hasta 2 decimales.'

        lineas.append({'id_producto': int(id_producto), 'cantidad': int(cantidad), 'precio': Decimal(precio)})

    return lineas, None
