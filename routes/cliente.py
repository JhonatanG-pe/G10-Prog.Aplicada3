"""
Módulo del CLIENTE: catálogo público, registro, login, carrito y checkout.
Blueprint completamente separado del personal interno (routes/administrador.py):
tiene su propia tabla (Cliente, no Usuario), su propio login, y su propia
sesión. Un cliente jamás comparte perfiles con Administrador/Operador.

Las contraseñas de Cliente usan el mismo esquema que Usuario: HASHBYTES
('SHA2_256', clave + salt) nativo de SQL Server, nunca el hash calculado
en Python (columna Clave VARBINARY(32) + columna Salt VARCHAR(32), ya
migradas por migracion_hashbytes.sql).

Procedimientos almacenados que usa (sql/stored_procedures.sql):
usp_Cliente_Login, usp_Cliente_CambiarClave y usp_Venta_Registrar (checkout).
"""
import re
from functools import wraps
from datetime import datetime
from decimal import Decimal
import pyodbc
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, current_app
from db import conexion_actual, consultar_todos, consultar_uno, resultado_procedimiento
from paginacion import consultar_pagina
from seguridad import SQL_HASH_CLAVE, validar_clave_nueva, clave_ingresada_es_comparable, generar_salt_unica
import facturacion
import comprobante_pdf

cliente_bp = Blueprint('cliente', __name__, url_prefix='/cliente')

MONTO_MAXIMO_VENTA = Decimal('99999999.99')  # columna Venta.MontoTotal DECIMAL(10,2)


def cliente_requerido(vista):
    """Protege una vista: solo un Cliente logueado puede pasar (nunca Administrador/Operador)."""
    @wraps(vista)
    def envoltura(*args, **kwargs):
        if not session.get('id_cliente'):
            flash('Debes iniciar sesión para continuar.', 'error')
            return redirect(url_for('cliente.login'))
        return vista(*args, **kwargs)
    return envoltura


# ==========================================================================
# CATÁLOGO PÚBLICO (no requiere login: cualquiera puede ver la tienda)
# ==========================================================================
@cliente_bp.route('/tienda')
def tienda():
    productos = consultar_pagina(
        """
        SELECT p.IdProducto, p.Nombre, p.Descripcion, p.Precio, p.Stock, p.RutaImagen,
               c.Descripcion AS Categoria, m.Descripcion AS Marca
        FROM Producto p
        LEFT JOIN Categoria c ON c.IdCategoria = p.IdCategoria
        LEFT JOIN Marca m ON m.IdMarca = p.IdMarca
        WHERE p.Activo = 1
        """,
        orden='p.Nombre, p.IdProducto', por_pagina=12,
    )
    return render_template('cliente/tienda.html', productos=productos)


# ==========================================================================
# REGISTRO
# ==========================================================================
@cliente_bp.route('/registro', methods=['GET', 'POST'])
def registro():
    if request.method == 'POST':
        datos = _leer_datos_personales()
        nombres, apellidos, telefono, domicilio = datos['nombres'], datos['apellidos'], datos['telefono'], datos['domicilio']
        correo = request.form.get('correo', '').strip()
        clave = request.form.get('clave', '')
        clave_confirmar = request.form.get('clave_confirmar', '')

        # El HTML ya restringe estos formatos, pero se vuelven a validar aquí porque
        # el navegador se puede saltar (formulario editado a mano, petición directa, etc.).
        error = datos['error']
        if not error and (not correo or not clave):
            error = 'Completa todos los campos obligatorios.'
        elif not error and (not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', correo) or len(correo) > 100):
            error = 'Ingresa un correo electrónico válido.'
        elif not error:
            error = validar_clave_nueva(clave, clave_confirmar)
        if error:
            flash(error, 'error')
            return render_template('cliente/registro.html')

        conexion = conexion_actual()
        cursor = conexion.cursor()

        cursor.execute("SELECT 1 FROM Cliente WHERE Correo = ?", correo)
        if cursor.fetchone():
            flash('Ya existe una cuenta con ese correo.', 'error')
            return render_template('cliente/registro.html')

        # Salt única de 32 caracteres; el hash real lo calcula SQL Server con
        # HASHBYTES, nunca Python (mismo esquema que Usuario, ver seguridad.py).
        try:
            salt = generar_salt_unica(cursor)
            cursor.execute(
                f"""
                INSERT INTO Cliente (Nombres, Apellidos, Correo, Clave, Salt, Telefono, Domicilio, Activo, FechaRegistro)
                OUTPUT INSERTED.IdCliente
                VALUES (?, ?, ?, {SQL_HASH_CLAVE}, ?, ?, ?, 1, ?)
                """,
                nombres, apellidos, correo, clave, salt, salt, telefono, domicilio, datetime.now(),
            )
            conexion.commit()
        except pyodbc.IntegrityError:
            # Dos registros simultáneos con el mismo correo: el índice único
            # UQ_Cliente_Correo de la BD rechaza el segundo (HU-13, criterio 2).
            conexion.rollback()
            flash('Ya existe una cuenta con ese correo.', 'error')
            return render_template('cliente/registro.html')
        flash('Cuenta creada correctamente. Ya puedes iniciar sesión.', 'success')
        return redirect(url_for('cliente.login'))

    return render_template('cliente/registro.html')


def _leer_datos_personales():
    """
    Nombres y apellidos (obligatorios, máx. 100), teléfono (opcional, 6 a 12 dígitos)
    y dirección (opcional, máx. 200): límites de las columnas de Cliente.
    Mismas reglas en el registro (HU-13) y en "Mi cuenta" (HU-21).
    """
    datos = {
        'nombres': ' '.join(request.form.get('nombres', '').split()),
        'apellidos': ' '.join(request.form.get('apellidos', '').split()),
        'telefono': request.form.get('telefono', '').strip() or None,
        'domicilio': ' '.join(request.form.get('domicilio', '').split()) or None,
        'error': None,
    }
    if not datos['nombres'] or not datos['apellidos']:
        datos['error'] = 'Completa todos los campos obligatorios.'
    elif len(datos['nombres']) > 100 or len(datos['apellidos']) > 100:
        datos['error'] = 'Nombres y apellidos admiten máximo 100 caracteres.'
    elif datos['telefono'] and not re.fullmatch(r'\d{6,12}', datos['telefono']):
        datos['error'] = 'El teléfono debe tener entre 6 y 12 dígitos numéricos.'
    elif datos['domicilio'] and len(datos['domicilio']) > 200:
        datos['error'] = 'La dirección admite máximo 200 caracteres.'
    return datos


# ==========================================================================
# LOGIN / LOGOUT
# ==========================================================================
@cliente_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        correo = request.form.get('correo', '').strip()
        clave_ingresada = request.form.get('clave', '')

        if not correo or not clave_ingresada_es_comparable(clave_ingresada):
            flash('Correo o contraseña incorrectos.', 'error')
            return render_template('cliente/login.html')

        # Procedimiento usp_Cliente_Login: el hash se recalcula en SQL Server con la salt
        # guardada del cliente; Python nunca ve el hash real ni registra la contraseña.
        fila = consultar_uno("EXEC dbo.usp_Cliente_Login @Correo = ?, @Clave = ?", correo, clave_ingresada)

        # Mismo mensaje si el correo no existe o la clave es incorrecta.
        if fila is None:
            flash('Correo o contraseña incorrectos.', 'error')
            return render_template('cliente/login.html')

        id_cliente, nombres, activo = fila

        # El estado se revela solo a quien ya demostró conocer la contraseña.
        if not activo:
            flash('Esta cuenta está inactiva.', 'error')
            return render_template('cliente/login.html')

        session.clear()  # cierra cualquier sesión anterior (de Administrador/Operador u otro cliente)
        session['id_cliente'] = id_cliente
        session['nombre_cliente'] = nombres

        return redirect(url_for('cliente.tienda'))

    return render_template('cliente/login.html')


@cliente_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('cliente.tienda'))


# ==========================================================================
# CARRITO
# ==========================================================================
@cliente_bp.route('/carrito/agregar/<int:id_producto>', methods=['POST'])
@cliente_requerido
def agregar_al_carrito(id_producto):
    cantidad = _leer_cantidad()
    if cantidad is None:
        flash('La cantidad debe ser un número entero mayor que 0.', 'error')
        return redirect(url_for('cliente.tienda'))

    conexion = conexion_actual()
    cursor = conexion.cursor()

    # El id llega desde la URL: nunca confiar en él. Solo se pueden agregar
    # productos que existan y estén activos (no dados de baja por el personal).
    cursor.execute(
        "SELECT Nombre, Stock FROM Producto WHERE IdProducto = ? AND Activo = 1",
        id_producto,
    )
    producto = cursor.fetchone()
    if producto is None:
        flash('El producto no existe o ya no está disponible.', 'error')
        return redirect(url_for('cliente.tienda'))

    # ¿Ya tiene este producto en el carrito? si sí, solo aumenta la cantidad.
    cursor.execute(
        "SELECT IdCarrito, Cantidad FROM Carrito WHERE IdCliente = ? AND IdProducto = ?",
        session['id_cliente'], id_producto,
    )
    fila = cursor.fetchone()

    cantidad_final = cantidad + (fila.Cantidad if fila else 0)
    if cantidad_final > producto.Stock:
        ya_tiene = f' Ya tienes {fila.Cantidad} en tu carrito.' if fila else ''
        flash(f'No hay suficiente stock de "{producto.Nombre}" (disponible: {producto.Stock}).{ya_tiene}', 'error')
        return redirect(url_for('cliente.tienda'))

    if fila:
        cursor.execute(
            "UPDATE Carrito SET Cantidad = ? WHERE IdCarrito = ?",
            fila.Cantidad + cantidad, fila.IdCarrito,
        )
    else:
        try:
            cursor.execute(
                "INSERT INTO Carrito (IdCliente, IdProducto, Cantidad) VALUES (?, ?, ?)",
                session['id_cliente'], id_producto, cantidad,
            )
        except pyodbc.IntegrityError:
            # Doble clic: la otra petición ya agregó este producto (índice único
            # UQ_Carrito_Cliente_Producto). No se duplica la fila del carrito.
            conexion.rollback()
            flash('Este producto ya se agregó a tu carrito. Revisa la cantidad allí.', 'error')
            return redirect(url_for('cliente.ver_carrito'))

    conexion.commit()
    flash('Producto agregado al carrito.', 'success')
    return redirect(url_for('cliente.tienda'))


@cliente_bp.route('/carrito')
@cliente_requerido
def ver_carrito():
    items = consultar_todos(
        """
        SELECT ca.IdCarrito, p.IdProducto, p.Nombre, p.Precio, p.RutaImagen, p.Stock,
               ca.Cantidad, (p.Precio * ca.Cantidad) AS Subtotal
        FROM Carrito ca
        INNER JOIN Producto p ON p.IdProducto = ca.IdProducto
        WHERE ca.IdCliente = ?
        ORDER BY p.Nombre
        """,
        session['id_cliente'],
    )

    total = sum(i.Subtotal for i in items) if items else 0
    return render_template('cliente/carrito.html', items=items, total=total)


@cliente_bp.route('/carrito/actualizar/<int:id_carrito>', methods=['POST'])
@cliente_requerido
def actualizar_carrito(id_carrito):
    cantidad = _leer_cantidad()
    if cantidad is None:
        flash('La cantidad debe ser un número entero mayor que 0. Para quitar el producto usa "Quitar".', 'error')
        return redirect(url_for('cliente.ver_carrito'))

    conexion = conexion_actual()
    cursor = conexion.cursor()
    # Solo un ítem del propio carrito del cliente (el IdCarrito llega en la URL).
    cursor.execute(
        """
        SELECT p.Nombre, p.Stock FROM Carrito ca
        INNER JOIN Producto p ON p.IdProducto = ca.IdProducto
        WHERE ca.IdCarrito = ? AND ca.IdCliente = ?
        """,
        id_carrito, session['id_cliente'],
    )
    item = cursor.fetchone()
    if item is None:
        flash('Ese producto ya no está en tu carrito.', 'error')
        return redirect(url_for('cliente.ver_carrito'))

    # HU-15: la cantidad no puede exceder el stock disponible del producto.
    if cantidad > item.Stock:
        flash(f'No puedes pedir {cantidad} de "{item.Nombre}": solo hay {item.Stock} disponible(s).', 'error')
        return redirect(url_for('cliente.ver_carrito'))

    cursor.execute(
        "UPDATE Carrito SET Cantidad = ? WHERE IdCarrito = ? AND IdCliente = ?",
        cantidad, id_carrito, session['id_cliente'],
    )
    conexion.commit()
    flash(f'Cantidad de "{item.Nombre}" actualizada.', 'success')
    return redirect(url_for('cliente.ver_carrito'))


@cliente_bp.route('/carrito/eliminar/<int:id_carrito>', methods=['POST'])
@cliente_requerido
def eliminar_del_carrito(id_carrito):
    conexion = conexion_actual()
    cursor = conexion.cursor()
    # Solo se borra ESE ítem y solo si es del propio cliente: los demás no se tocan (HU-15).
    cursor.execute(
        "DELETE FROM Carrito WHERE IdCarrito = ? AND IdCliente = ?",
        id_carrito, session['id_cliente'],
    )
    borrado = cursor.rowcount
    conexion.commit()
    if borrado:
        flash('Producto quitado del carrito.', 'success')
    else:
        flash('Ese producto ya no está en tu carrito.', 'error')
    return redirect(url_for('cliente.ver_carrito'))


def _leer_cantidad():
    """Cantidad enviada en el formulario: entero de 1 en adelante (columna INT), o None si no es válida."""
    cantidad = request.form.get('cantidad', '').strip()
    if not re.fullmatch(r'\d{1,9}', cantidad) or int(cantidad) == 0:
        return None
    return int(cantidad)


# ==========================================================================
# CHECKOUT
# ==========================================================================
def _mensaje_venta_rechazada(resultado):
    """Mensaje para el cliente según el código que devuelve usp_Venta_Registrar."""
    if resultado.Resultado == 1:
        return 'Tu carrito cambió mientras se confirmaba el pedido. Revísalo e inténtalo de nuevo.'
    if resultado.Resultado == 2:
        return f'"{resultado.Producto}" ya no está disponible. Quítalo del carrito para continuar.'
    if resultado.Resultado == 3 and resultado.Producto:
        return f'"{resultado.Producto}" ya no tiene stock suficiente (disponible: {resultado.Disponible}); revisa tu carrito.'
    if resultado.Resultado == 3:
        return 'Un producto de tu carrito ya no tiene stock suficiente; revisa tu carrito.'
    if resultado.Resultado == 4:
        return f'El total del pedido supera el máximo permitido (S/ {MONTO_MAXIMO_VENTA}).'
    if resultado.Resultado == 6:
        return 'Los datos del comprobante no son válidos. Revisa el RUC/DNI y vuelve a intentarlo.'
    if resultado.Resultado == 7:
        return 'Por ahora no se puede emitir ese tipo de comprobante. Elige el otro o inténtalo más tarde.'
    return 'Tu carrito cambió mientras se confirmaba el pedido. Revísalo e inténtalo de nuevo.'

def _leer_comprobante(total):
    """
    Datos del comprobante elegido en el checkout. Devuelve (datos, error).
      Boleta (03):  DNI opcional; obligatorio si el total supera S/ 700 (regla de SUNAT).
      Factura (01): RUC válido (dígito verificador), razón social y dirección fiscal opcional.
    """
    tipo = request.form.get('tipo_comprobante', '03')
    if tipo == '01':
        ruc = request.form.get('ruc', '').strip()
        razon_social = ' '.join(request.form.get('razon_social', '').split()).upper()
        direccion_fiscal = ' '.join(request.form.get('direccion_fiscal', '').split()).upper() or None
        if not facturacion.ruc_valido(ruc):
            return None, 'El RUC no es válido: debe tener 11 dígitos y su dígito verificador debe ser correcto.'
        if not razon_social:
            return None, 'Para emitir una factura, la razón social es obligatoria.'
        if len(razon_social) > 200:
            return None, 'La razón social admite máximo 200 caracteres.'
        if direccion_fiscal and len(direccion_fiscal) > 300:
            return None, 'La dirección fiscal admite máximo 300 caracteres.'
        return {'tipo': '01', 'tipo_doc': '6', 'num_doc': ruc, 'nombre': razon_social,
                'direccion': direccion_fiscal}, None
    if tipo != '03':
        return None, 'Elige Boleta o Factura.'
    dni = request.form.get('dni', '').strip()
    if dni and not facturacion.dni_valido(dni):
        return None, 'El DNI debe tener 8 dígitos.'
    if not dni and total > facturacion.MONTO_MAXIMO_BOLETA_SIN_DOCUMENTO:
        return None, (f'Para boletas de más de S/ {facturacion.MONTO_MAXIMO_BOLETA_SIN_DOCUMENTO} '
                      'SUNAT exige el DNI del comprador.')
    # Nombre en la boleta: el de la cuenta del cliente (lo completa usp_Venta_Registrar).
    return {'tipo': '03', 'tipo_doc': '1' if dni else '0', 'num_doc': dni or '-', 'nombre': None,
            'direccion': None}, None


@cliente_bp.route('/carrito/finalizar', methods=['GET', 'POST'])
@cliente_requerido
def finalizar_compra():
    conexion = conexion_actual()
    cursor = conexion.cursor()
    cursor.execute(
        """
        SELECT ca.IdCarrito, p.IdProducto, p.Nombre, p.Precio, p.Stock, p.Activo, ca.Cantidad
        FROM Carrito ca
        INNER JOIN Producto p ON p.IdProducto = ca.IdProducto
        WHERE ca.IdCliente = ?
        """,
        session['id_cliente'],
    )
    items = cursor.fetchall()

    if not items:
        flash('Tu carrito está vacío.', 'error')
        return redirect(url_for('cliente.tienda'))

    total = sum((i.Precio * i.Cantidad for i in items), Decimal('0'))

    if request.method == 'POST':
        contacto = ' '.join(request.form.get('contacto', '').split())
        telefono = request.form.get('telefono', '').strip() or None
        direccion = ' '.join(request.form.get('direccion', '').split())

        # HU-16: datos de entrega completos. Límites = columnas de la tabla Venta.
        # El HTML ya los restringe, pero se validan aquí porque el navegador se puede saltar.
        error = None
        if not contacto or not direccion:
            error = 'El nombre de contacto y la dirección de entrega son obligatorios.'
        elif len(contacto) > 100:
            error = 'El nombre de contacto admite máximo 100 caracteres.'
        elif len(direccion) > 300:
            error = 'La dirección admite máximo 300 caracteres.'
        elif telefono and not re.fullmatch(r'\d{6,12}', telefono):
            error = 'El teléfono debe tener entre 6 y 12 dígitos numéricos.'
        elif total > MONTO_MAXIMO_VENTA:
            error = f'El total del pedido supera el máximo permitido (S/ {MONTO_MAXIMO_VENTA}).'
        comprobante = None
        if not error:
            comprobante, error = _leer_comprobante(total)
        if error:
            flash(error, 'error')
            return render_template('cliente/checkout.html', items=items, total=total, entrega=request.form)

        # Revalida el stock antes de confirmar: puede haber cambiado desde
        # que se agregó al carrito (otro cliente pudo haberlo comprado antes).
        for item in items:
            # El personal pudo dar de baja el producto después de que se agregó al carrito.
            if not item.Activo:
                flash(f'"{item.Nombre}" ya no está disponible. Quítalo del carrito para continuar.', 'error')
                return redirect(url_for('cliente.ver_carrito'))
            if item.Cantidad > item.Stock:
                flash(f'No hay suficiente stock de "{item.Nombre}" (disponible: {item.Stock}).', 'error')
                return redirect(url_for('cliente.ver_carrito'))

        try:
            # Procedimiento almacenado usp_Venta_Registrar (sql/stored_procedures.sql): en una
            # sola transacción vuelve a leer y bloquear el carrito, registra la Venta y su
            # Detalle_Venta, descuenta el stock (verificando y descontando en un solo paso:
            # nunca queda negativo), vacía el carrito y crea el comprobante electrónico
            # (serie, correlativo, valor de venta e IGV). Si algo no cuadra, no graba nada.
            cursor.execute(
                "EXEC dbo.usp_Venta_Registrar @IdCliente = ?, @Contacto = ?, @Telefono = ?, "
                "@Direccion = ?, @FechaVenta = ?, @TipoComprobante = ?, @ClienteTipoDoc = ?, "
                "@ClienteNumDoc = ?, @ClienteNombre = ?, @ClienteDireccion = ?",
                session['id_cliente'], contacto, telefono, direccion, datetime.now(),
                comprobante['tipo'], comprobante['tipo_doc'], comprobante['num_doc'],
                comprobante['nombre'], comprobante['direccion'],
            )
            resultado = resultado_procedimiento(cursor)
            if resultado.Resultado != 0:
                conexion.rollback()
                flash(_mensaje_venta_rechazada(resultado), 'error')
                if resultado.Resultado in (6, 7):
                    return render_template('cliente/checkout.html', items=items, total=total, entrega=request.form)
                return redirect(url_for('cliente.ver_carrito'))
            conexion.commit()
        except Exception:
            conexion.rollback()
            # El detalle técnico va al log del servidor, nunca a la pantalla del cliente.
            current_app.logger.exception('Error al procesar la compra del cliente %s', session['id_cliente'])
            flash('No se pudo procesar la compra. Inténtalo nuevamente en unos minutos.', 'error')
            return redirect(url_for('cliente.ver_carrito'))

        # La venta ya está confirmada. El envío a SUNAT va aparte: si SUNAT no responde,
        # la compra NO se pierde; el comprobante queda pendiente y se reenvía desde el panel.
        aviso = f'Comprobante {resultado.NumeroComprobante} pendiente de envío a SUNAT.'
        if facturacion.envio_automatico():
            try:
                aviso = 'Comprobante ' + facturacion.texto_estado(facturacion.emitir(conexion, resultado.IdComprobante))
            except Exception:
                conexion.rollback()
                current_app.logger.exception('Error al enviar a SUNAT el comprobante %s', resultado.IdComprobante)
        flash(f'¡Compra realizada correctamente! N° de pedido: {resultado.IdVenta}. {aviso}', 'success')
        return redirect(url_for('cliente.ver_pedido', id_venta=resultado.IdVenta))

    # Se proponen los datos que el cliente ya registró (los puede cambiar para esta entrega).
    cursor.execute("SELECT Nombres, Apellidos, Telefono, Domicilio FROM Cliente WHERE IdCliente = ?", session['id_cliente'])
    cliente = cursor.fetchone()
    entrega = {
        'contacto': f'{cliente.Nombres} {cliente.Apellidos}',
        'telefono': cliente.Telefono or '',
        'direccion': cliente.Domicilio or '',
    }
    return render_template('cliente/checkout.html', items=items, total=total, entrega=entrega)


# ==========================================================================
# HU-19: MIS PEDIDOS
# ==========================================================================
@cliente_bp.route('/pedidos')
@cliente_requerido
def mis_pedidos():
    """Solo los pedidos del cliente de la sesión, del más reciente al más antiguo."""
    pedidos = consultar_pagina(
        """
        SELECT IdVenta, FechaVenta, TotalProducto, MontoTotal, Estado
        FROM Venta
        WHERE IdCliente = ?
        """,
        session['id_cliente'],
        orden='FechaVenta DESC, IdVenta DESC', por_pagina=10,
    )
    return render_template('cliente/mis_pedidos.html', pedidos=pedidos)


@cliente_bp.route('/pedidos/<int:id_venta>')
@cliente_requerido
def ver_pedido(id_venta):
    conexion = conexion_actual()
    cursor = conexion.cursor()
    # El filtro por IdCliente impide ver el pedido de otro cliente cambiando el
    # número en la URL (HU-19, criterio 3): para él ese pedido "no existe".
    cursor.execute(
        """
        SELECT IdVenta, FechaVenta, TotalProducto, MontoTotal, Contacto, Telefono, Direccion, Estado
        FROM Venta WHERE IdVenta = ? AND IdCliente = ?
        """,
        id_venta, session['id_cliente'],
    )
    pedido = cursor.fetchone()
    if pedido is None:
        flash('Ese pedido no existe o no te pertenece.', 'error')
        return redirect(url_for('cliente.mis_pedidos'))

    cursor.execute(
        """
        SELECT p.Nombre, p.RutaImagen, dv.Cantidad, dv.Precio, (dv.Cantidad * dv.Precio) AS Subtotal
        FROM Detalle_Venta dv
        INNER JOIN Producto p ON p.IdProducto = dv.IdProducto
        WHERE dv.IdVenta = ?
        ORDER BY p.Nombre
        """,
        id_venta,
    )
    detalle = cursor.fetchall()
    comprobante, _ = facturacion.obtener(cursor, id_venta=id_venta, id_cliente=session['id_cliente'])
    return render_template('cliente/pedido_detalle.html', pedido=pedido, detalle=detalle,
                           comprobante=comprobante, ambito='cliente')


@cliente_bp.route('/pedidos/<int:id_venta>/comprobante')
@cliente_requerido
def comprobante_cliente(id_venta):
    """Representación impresa de la boleta o factura (solo del pedido del propio cliente)."""
    pagina = facturacion.pagina_impresa(conexion_actual().cursor(), id_venta, id_cliente=session['id_cliente'])
    if pagina is None:
        flash('Ese comprobante no existe o no te pertenece.', 'error')
        return redirect(url_for('cliente.mis_pedidos'))
    return pagina


@cliente_bp.route('/pedidos/<int:id_venta>/comprobante/pdf')
@cliente_requerido
def pdf_comprobante_cliente(id_venta):
    """PDF de la boleta o factura: se guarda en la carpeta local y se descarga."""
    archivo = comprobante_pdf.respuesta_pdf(conexion_actual().cursor(), id_venta, id_cliente=session['id_cliente'])
    if archivo is None:
        flash('Ese comprobante no existe o no te pertenece.', 'error')
        return redirect(url_for('cliente.mis_pedidos'))
    return archivo


@cliente_bp.route('/pedidos/<int:id_venta>/comprobante/<tipo>')
@cliente_requerido
def archivo_comprobante_cliente(id_venta, tipo):
    """Descarga del XML firmado (tipo=xml) o de la constancia de SUNAT (tipo=cdr)."""
    archivo = facturacion.respuesta_archivo(conexion_actual().cursor(), id_venta, tipo, id_cliente=session['id_cliente'])
    if archivo is None:
        flash('Ese archivo no existe o no te pertenece.', 'error')
        return redirect(url_for('cliente.mis_pedidos'))
    return archivo


# ==========================================================================
# HU-21: MI CUENTA
# ==========================================================================
@cliente_bp.route('/cuenta', methods=['GET', 'POST'])
@cliente_requerido
def mi_cuenta():
    conexion = conexion_actual()
    cursor = conexion.cursor()
    cursor.execute(
        "SELECT Nombres, Apellidos, Correo, Telefono, Domicilio FROM Cliente WHERE IdCliente = ?",
        session['id_cliente'],
    )
    cliente = cursor.fetchone()

    if request.method == 'POST':
        datos = _leer_datos_personales()
        if datos['error']:
            flash(datos['error'], 'error')
            return render_template('cliente/mi_cuenta.html', cliente=cliente, datos=request.form)

        # El correo NO se actualiza (HU-21): aunque se envíe en el formulario, se ignora.
        cursor.execute(
            "UPDATE Cliente SET Nombres = ?, Apellidos = ?, Telefono = ?, Domicilio = ? WHERE IdCliente = ?",
            datos['nombres'], datos['apellidos'], datos['telefono'], datos['domicilio'], session['id_cliente'],
        )
        conexion.commit()
        session['nombre_cliente'] = datos['nombres']   # el nombre actualizado se ve en la sesión
        flash('Tus datos se actualizaron correctamente.', 'success')
        return redirect(url_for('cliente.mi_cuenta'))

    return render_template('cliente/mi_cuenta.html', cliente=cliente, datos=None)


@cliente_bp.route('/cuenta/clave', methods=['POST'])
@cliente_requerido
def cambiar_clave():
    clave_actual = request.form.get('clave_actual', '')
    clave_nueva = request.form.get('clave_nueva', '')
    clave_confirmar = request.form.get('clave_confirmar', '')

    if not clave_actual or not clave_ingresada_es_comparable(clave_actual):
        flash('La contraseña actual es incorrecta.', 'error')
        return redirect(url_for('cliente.mi_cuenta'))
    error = validar_clave_nueva(clave_nueva, clave_confirmar)
    if error:
        flash(error, 'error')
        return redirect(url_for('cliente.mi_cuenta'))

    conexion = conexion_actual()
    cursor = conexion.cursor()
    # Procedimiento usp_Cliente_CambiarClave: verifica la contraseña actual recalculando el
    # HASHBYTES en SQL Server (HU-05) y, solo si coincide, guarda la nueva con una salt
    # NUEVA (HU-21, criterio 2), todo en una sola instrucción.
    salt = generar_salt_unica(cursor)
    cursor.execute(
        "EXEC dbo.usp_Cliente_CambiarClave @IdCliente = ?, @ClaveActual = ?, @ClaveNueva = ?, @SaltNueva = ?",
        session['id_cliente'], clave_actual, clave_nueva, salt,
    )
    if resultado_procedimiento(cursor).Resultado != 1:
        conexion.rollback()
        flash('La contraseña actual es incorrecta.', 'error')
        return redirect(url_for('cliente.mi_cuenta'))
    conexion.commit()
    flash('Tu contraseña se cambió correctamente.', 'success')
    return redirect(url_for('cliente.mi_cuenta'))
