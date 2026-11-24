"""
Módulo de PEDIDOS (HU-20): el personal ve los pedidos de la tienda (tabla Venta)
y actualiza su estado. Quién entra lo decide la opción de menú "Pedidos"
asignada a cada perfil en "Asignar Accesos" (acceso_requerido).

Ciclo de vida del pedido (columna Venta.Estado):
    Pendiente --> Entregado   (final)
    Pendiente --> Anulado     (final; devuelve el stock de cada producto)
"""
from flask import render_template, request, redirect, url_for, flash, current_app
from db import conexion_actual, resultado_procedimiento
from paginacion import consultar_pagina
from routes.administrador import admin_bp, acceso_requerido
import facturacion
import comprobante_pdf

ESTADO_PENDIENTE = 'Pendiente'
ESTADO_ENTREGADO = 'Entregado'
ESTADO_ANULADO = 'Anulado'
ESTADOS = (ESTADO_PENDIENTE, ESTADO_ENTREGADO, ESTADO_ANULADO)


@admin_bp.route('/pedidos')
@acceso_requerido
def lista_pedidos():
    estado = request.args.get('estado', '').strip()
    # El filtro llega en la URL: solo se acepta uno de los estados conocidos.
    if estado and estado not in ESTADOS:
        flash('El estado elegido no es válido.', 'error')
        return redirect(url_for('admin.lista_pedidos'))

    pedidos = consultar_pagina(
        f"""
        SELECT v.IdVenta, v.FechaVenta, cl.Nombres, cl.Apellidos, v.TotalProducto, v.MontoTotal, v.Estado
        FROM Venta v
        INNER JOIN Cliente cl ON cl.IdCliente = v.IdCliente
        {'WHERE v.Estado = ?' if estado else ''}
        """,
        *([estado] if estado else []),
        orden='v.FechaVenta DESC, v.IdVenta DESC',
    )
    return render_template('admin/pedidos.html', pedidos=pedidos, estados=ESTADOS, estado=estado)


@admin_bp.route('/pedidos/<int:id_venta>')
@acceso_requerido
def ver_pedido_admin(id_venta):
    conexion = conexion_actual()
    cursor = conexion.cursor()
    cursor.execute(
        """
        SELECT v.IdVenta, v.FechaVenta, v.TotalProducto, v.MontoTotal, v.Contacto, v.Telefono,
               v.Direccion, v.Estado, cl.Nombres, cl.Apellidos, cl.Correo
        FROM Venta v
        INNER JOIN Cliente cl ON cl.IdCliente = v.IdCliente
        WHERE v.IdVenta = ?
        """,
        id_venta,
    )
    pedido = cursor.fetchone()
    if pedido is None:
        flash('El pedido no existe.', 'error')
        return redirect(url_for('admin.lista_pedidos'))

    cursor.execute(
        """
        SELECT p.Nombre, dv.Cantidad, dv.Precio, (dv.Cantidad * dv.Precio) AS Subtotal
        FROM Detalle_Venta dv
        INNER JOIN Producto p ON p.IdProducto = dv.IdProducto
        WHERE dv.IdVenta = ?
        ORDER BY p.Nombre
        """,
        id_venta,
    )
    detalle = cursor.fetchall()
    comprobante, _ = facturacion.obtener(cursor, id_venta=id_venta)
    return render_template('admin/pedido_detalle.html', pedido=pedido, detalle=detalle,
                           pendiente=pedido.Estado == ESTADO_PENDIENTE,
                           comprobante=comprobante, ambito='admin')


# --------------------------------------------------------------------------
# Comprobante electrónico del pedido (boleta / factura SUNAT)
# --------------------------------------------------------------------------
@admin_bp.route('/pedidos/<int:id_venta>/comprobante')
@acceso_requerido
def comprobante_admin(id_venta):
    pagina = facturacion.pagina_impresa(conexion_actual().cursor(), id_venta)
    if pagina is None:
        flash('Ese pedido no tiene comprobante electrónico.', 'error')
        return redirect(url_for('admin.ver_pedido_admin', id_venta=id_venta))
    return pagina


@admin_bp.route('/pedidos/<int:id_venta>/comprobante/pdf')
@acceso_requerido
def pdf_comprobante_admin(id_venta):
    """PDF de la boleta o factura: se guarda en la carpeta local y se descarga."""
    archivo = comprobante_pdf.respuesta_pdf(conexion_actual().cursor(), id_venta)
    if archivo is None:
        flash('Ese pedido no tiene comprobante electrónico.', 'error')
        return redirect(url_for('admin.ver_pedido_admin', id_venta=id_venta))
    return archivo


@admin_bp.route('/pedidos/<int:id_venta>/comprobante/<tipo>')
@acceso_requerido
def archivo_comprobante_admin(id_venta, tipo):
    archivo = facturacion.respuesta_archivo(conexion_actual().cursor(), id_venta, tipo)
    if archivo is None:
        flash('Ese archivo no existe.', 'error')
        return redirect(url_for('admin.ver_pedido_admin', id_venta=id_venta))
    return archivo


@admin_bp.route('/pedidos/<int:id_venta>/comprobante/enviar', methods=['POST'])
@acceso_requerido
def enviar_comprobante(id_venta):
    """Envía (o reenvía) a SUNAT un comprobante que quedó pendiente o con error de envío."""
    conexion = conexion_actual()
    comprobante, _ = facturacion.obtener(conexion.cursor(), id_venta=id_venta)
    if comprobante is None:
        flash('Ese pedido no tiene comprobante electrónico.', 'error')
        return redirect(url_for('admin.ver_pedido_admin', id_venta=id_venta))
    if comprobante.EstadoSunat in facturacion.ESTADOS_DEFINITIVOS:
        flash(f'El comprobante {comprobante.NumeroComprobante} ya tiene respuesta de SUNAT: no se reenvía.', 'error')
        return redirect(url_for('admin.ver_pedido_admin', id_venta=id_venta))
    try:
        comprobante = facturacion.emitir(conexion, comprobante.IdComprobante)
    except Exception:
        conexion.rollback()
        current_app.logger.exception('Error al enviar a SUNAT el comprobante del pedido %s', id_venta)
        flash('No se pudo enviar el comprobante a SUNAT. Inténtalo nuevamente.', 'error')
        return redirect(url_for('admin.ver_pedido_admin', id_venta=id_venta))
    categoria = 'success' if comprobante.EstadoSunat in ('ACEPTADO', 'OBSERVADO') else 'error'
    flash('Comprobante ' + facturacion.texto_estado(comprobante), categoria)
    return redirect(url_for('admin.ver_pedido_admin', id_venta=id_venta))


@admin_bp.route('/pedidos/<int:id_venta>/estado', methods=['POST'])
@acceso_requerido
def cambiar_estado_pedido(id_venta):
    acciones = {'entregar': ESTADO_ENTREGADO, 'anular': ESTADO_ANULADO}
    nuevo_estado = acciones.get(request.form.get('accion', ''))
    if nuevo_estado is None:
        flash('La acción solicitada no es válida.', 'error')
        return redirect(url_for('admin.ver_pedido_admin', id_venta=id_venta))

    conexion = conexion_actual()
    cursor = conexion.cursor()
    try:
        # Procedimiento usp_Venta_CambiarEstado (sql/stored_procedures.sql): solo cambia si
        # el pedido SIGUE en Pendiente (HU-20, criterio 4; si dos usuarios actúan a la vez,
        # solo uno lo consigue) y, al anularlo, devuelve el stock de cada producto en la
        # MISMA transacción (criterio 3).
        cursor.execute("EXEC dbo.usp_Venta_CambiarEstado @IdVenta = ?, @NuevoEstado = ?", id_venta, nuevo_estado)
        if resultado_procedimiento(cursor).Resultado != 0:
            conexion.rollback()
            flash('Este pedido ya no está Pendiente: un pedido Entregado o Anulado no puede cambiar de estado.', 'error')
            return redirect(url_for('admin.ver_pedido_admin', id_venta=id_venta))
        conexion.commit()
    except Exception:
        conexion.rollback()
        current_app.logger.exception('Error al cambiar el estado del pedido %s', id_venta)
        flash('No se pudo actualizar el pedido; no se guardó ningún cambio. Inténtalo nuevamente.', 'error')
        return redirect(url_for('admin.ver_pedido_admin', id_venta=id_venta))

    if nuevo_estado == ESTADO_ANULADO:
        flash(f'Pedido N° {id_venta} anulado. El stock de sus productos fue devuelto.', 'success')
    else:
        flash(f'Pedido N° {id_venta} marcado como entregado.', 'success')
    return redirect(url_for('admin.ver_pedido_admin', id_venta=id_venta))
