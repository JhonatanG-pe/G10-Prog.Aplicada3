"""
Módulo de catálogo: Categorías, Marcas y Productos.
Estas rutas se agregan al mismo Blueprint 'admin' (prefijo /admin),
así que comparten el login y la protección de administrador.py.

Imágenes de producto: por pedido del profesor, la foto NUNCA se guarda en
binario dentro de la base de datos (eso pesaría mucho el motor). Se sube como
archivo a la carpeta de fotos del HOSTING (almacen_fotos.py, por FTP), con un
nombre único (para que dos fotos distintas nunca se pisen entre sí), y en la
tabla Producto solo se guardan dos columnas de texto:
    RutaImagen   -> dirección pública de la foto para el <img src="...">
    NombreImagen -> nombre real del archivo (para borrarlo si se reemplaza)
Así todas las PC ven las mismas fotos (antes quedaban en la PC de quien las subía).
"""
import os
import re
import uuid
from decimal import Decimal
from types import SimpleNamespace
from flask import render_template, request, redirect, url_for, flash, session, current_app
from werkzeug.utils import secure_filename
from db import conexion_actual
from paginacion import consultar_pagina
import almacen_fotos
from routes.administrador import admin_bp, acceso_requerido

EXTENSIONES_PERMITIDAS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
# HU-09: tamaño máximo de la foto. 2 MB alcanzan de sobra para una foto de catálogo
# (una de 1200x1200 en JPG pesa ~300 KB) y evitan llenar el disco del hosting.
TAMANO_MAXIMO_IMAGEN_MB = 2


@admin_bp.context_processor
def _datos_formulario_producto():
    """Hace visible el límite de la foto en las plantillas del panel (texto de ayuda y validación JS)."""
    return {'tamano_maximo_mb': TAMANO_MAXIMO_IMAGEN_MB}
CARPETA_PRODUCTOS = os.path.join('static', 'uploads', 'productos')


# ==========================================================================
# CATEGORÍAS
# ==========================================================================
@admin_bp.route('/categorias')
@acceso_requerido
def lista_categorias():
    categorias = consultar_pagina(
        """
        SELECT c.IdCategoria, c.Descripcion, c.Activo,
               (SELECT COUNT(*) FROM Producto p WHERE p.IdCategoria = c.IdCategoria) AS Productos
        FROM Categoria c
        """,
        orden='c.Descripcion, c.IdCategoria',
    )
    return render_template('admin/categorias.html', categorias=categorias)


@admin_bp.route('/categorias/nueva', methods=['GET', 'POST'])
@acceso_requerido
def nueva_categoria():
    if request.method == 'POST':
        conexion = conexion_actual()
        cursor = conexion.cursor()
        descripcion = _leer_descripcion_categoria(cursor, id_categoria=None)
        if descripcion is None:
            return render_template('admin/categoria_form.html', categoria=None)

        cursor.execute("INSERT INTO Categoria (Descripcion, Activo) VALUES (?, 1)", descripcion)
        conexion.commit()
        flash('Categoría creada correctamente. Ya puedes asignarla a tus productos.', 'success')
        return redirect(url_for('admin.lista_categorias'))

    return render_template('admin/categoria_form.html', categoria=None)


@admin_bp.route('/categorias/editar/<int:id_categoria>', methods=['GET', 'POST'])
@acceso_requerido
def editar_categoria(id_categoria):
    conexion = conexion_actual()
    cursor = conexion.cursor()
    cursor.execute("SELECT IdCategoria, Descripcion, Activo FROM Categoria WHERE IdCategoria = ?", id_categoria)
    categoria = cursor.fetchone()
    if categoria is None:
        flash('La categoría no existe.', 'error')
        return redirect(url_for('admin.lista_categorias'))

    if request.method == 'POST':
        descripcion = _leer_descripcion_categoria(cursor, id_categoria=id_categoria)
        if descripcion is None:
            return render_template('admin/categoria_form.html', categoria=categoria)

        # Solo se edita el nombre. Como los productos guardan el IdCategoria (no el
        # texto), el nombre nuevo se refleja al instante en todos sus productos (HU-06).
        # Anular/reactivar se hace con los botones de la lista, que validan los productos.
        cursor.execute("UPDATE Categoria SET Descripcion = ? WHERE IdCategoria = ?", descripcion, id_categoria)
        conexion.commit()
        flash('Categoría actualizada. El cambio se refleja en todos sus productos.', 'success')
        return redirect(url_for('admin.lista_categorias'))

    return render_template('admin/categoria_form.html', categoria=categoria)


@admin_bp.route('/categorias/estado/<int:id_categoria>', methods=['POST'])
@acceso_requerido
def cambiar_estado_categoria(id_categoria):
    """Anula (Activo = 0) o reactiva (1) una categoría. Nunca se borra."""
    reactivar = request.form.get('accion') == 'reactivar'
    conexion = conexion_actual()
    cursor = conexion.cursor()

    cursor.execute("SELECT Descripcion FROM Categoria WHERE IdCategoria = ?", id_categoria)
    categoria = cursor.fetchone()
    if categoria is None:
        flash('La categoría no existe.', 'error')
        return redirect(url_for('admin.lista_categorias'))

    if not reactivar:
        # Regla HU-06: una categoría con productos asociados no se puede anular;
        # sus productos quedarían apuntando a una categoría que ya no se ofrece.
        cursor.execute("SELECT COUNT(*) FROM Producto WHERE IdCategoria = ?", id_categoria)
        productos = cursor.fetchone()[0]
        if productos:
            flash(f'No se puede anular la categoría "{categoria.Descripcion}" porque tiene {productos} '
                  f'producto(s) asociado(s). Primero cámbialos a otra categoría.', 'error')
            return redirect(url_for('admin.lista_categorias'))

    cursor.execute("UPDATE Categoria SET Activo = ? WHERE IdCategoria = ?", 1 if reactivar else 0, id_categoria)
    conexion.commit()
    flash(f'Categoría "{categoria.Descripcion}" ' + ('reactivada.' if reactivar else 'anulada.'), 'success')
    return redirect(url_for('admin.lista_categorias'))


def _leer_descripcion_categoria(cursor, id_categoria):
    """
    Valida el nombre de la categoría (obligatorio, máximo 100 caracteres como la
    columna, y sin repetirse). id_categoria=None es alta nueva; al editar se excluye
    a sí misma. Devuelve la descripción, o None si no es válida (ya con el flash).
    """
    descripcion = ' '.join(request.form.get('descripcion', '').split())  # quita espacios dobles
    if not descripcion:
        flash('El nombre de la categoría es obligatorio.', 'error')
        return None
    if len(descripcion) > 100:
        flash('El nombre de la categoría admite máximo 100 caracteres.', 'error')
        return None

    # La BD compara sin distinguir mayúsculas: "celulares" = "Celulares".
    cursor.execute(
        "SELECT Descripcion FROM Categoria WHERE Descripcion = ? AND IdCategoria <> ?",
        descripcion, id_categoria or 0,
    )
    duplicada = cursor.fetchone()
    if duplicada:
        flash(f'Ya existe la categoría "{duplicada.Descripcion}".', 'error')
        return None
    return descripcion


# ==========================================================================
# MARCAS (mismo patrón que Categorías)
# ==========================================================================
@admin_bp.route('/marcas')
@acceso_requerido
def lista_marcas():
    marcas = consultar_pagina(
        """
        SELECT m.IdMarca, m.Descripcion, m.Activo,
               (SELECT COUNT(*) FROM Producto p WHERE p.IdMarca = m.IdMarca) AS Productos
        FROM Marca m
        """,
        orden='m.Descripcion, m.IdMarca',
    )
    return render_template('admin/marcas.html', marcas=marcas)


@admin_bp.route('/marcas/nueva', methods=['GET', 'POST'])
@acceso_requerido
def nueva_marca():
    if request.method == 'POST':
        conexion = conexion_actual()
        cursor = conexion.cursor()
        descripcion = _leer_descripcion_marca(cursor, id_marca=None)
        if descripcion is None:
            return render_template('admin/marca_form.html', marca=None)

        cursor.execute("INSERT INTO Marca (Descripcion, Activo) VALUES (?, 1)", descripcion)
        conexion.commit()
        flash('Marca creada correctamente. Ya puedes asignarla a tus productos.', 'success')
        return redirect(url_for('admin.lista_marcas'))

    return render_template('admin/marca_form.html', marca=None)


@admin_bp.route('/marcas/editar/<int:id_marca>', methods=['GET', 'POST'])
@acceso_requerido
def editar_marca(id_marca):
    conexion = conexion_actual()
    cursor = conexion.cursor()
    cursor.execute("SELECT IdMarca, Descripcion, Activo FROM Marca WHERE IdMarca = ?", id_marca)
    marca = cursor.fetchone()
    if marca is None:
        flash('La marca no existe.', 'error')
        return redirect(url_for('admin.lista_marcas'))

    if request.method == 'POST':
        descripcion = _leer_descripcion_marca(cursor, id_marca=id_marca)
        if descripcion is None:
            return render_template('admin/marca_form.html', marca=marca)

        # Solo se edita el nombre. Como los productos guardan el IdMarca (no el
        # texto), el nombre nuevo se refleja al instante en todos sus productos (HU-07).
        # Anular/reactivar se hace con los botones de la lista, que validan los productos.
        cursor.execute("UPDATE Marca SET Descripcion = ? WHERE IdMarca = ?", descripcion, id_marca)
        conexion.commit()
        flash('Marca actualizada. El cambio se refleja en todos sus productos.', 'success')
        return redirect(url_for('admin.lista_marcas'))

    return render_template('admin/marca_form.html', marca=marca)


@admin_bp.route('/marcas/estado/<int:id_marca>', methods=['POST'])
@acceso_requerido
def cambiar_estado_marca(id_marca):
    """Anula (Activo = 0) o reactiva (1) una marca. Nunca se borra."""
    reactivar = request.form.get('accion') == 'reactivar'
    conexion = conexion_actual()
    cursor = conexion.cursor()

    cursor.execute("SELECT Descripcion FROM Marca WHERE IdMarca = ?", id_marca)
    marca = cursor.fetchone()
    if marca is None:
        flash('La marca no existe.', 'error')
        return redirect(url_for('admin.lista_marcas'))

    if not reactivar:
        # Regla HU-07: una marca con productos asociados no se puede anular.
        cursor.execute("SELECT COUNT(*) FROM Producto WHERE IdMarca = ?", id_marca)
        productos = cursor.fetchone()[0]
        if productos:
            flash(f'No se puede anular la marca "{marca.Descripcion}" porque tiene {productos} '
                  f'producto(s) asociado(s). Primero cámbialos a otra marca.', 'error')
            return redirect(url_for('admin.lista_marcas'))

    cursor.execute("UPDATE Marca SET Activo = ? WHERE IdMarca = ?", 1 if reactivar else 0, id_marca)
    conexion.commit()
    flash(f'Marca "{marca.Descripcion}" ' + ('reactivada.' if reactivar else 'anulada.'), 'success')
    return redirect(url_for('admin.lista_marcas'))


def _leer_descripcion_marca(cursor, id_marca):
    """
    Valida el nombre de la marca (obligatorio, máximo 100 caracteres como la
    columna, y sin repetirse). id_marca=None es alta nueva; al editar se excluye
    a sí misma. Devuelve la descripción, o None si no es válida (ya con el flash).
    """
    descripcion = ' '.join(request.form.get('descripcion', '').split())  # quita espacios dobles
    if not descripcion:
        flash('El nombre de la marca es obligatorio.', 'error')
        return None
    if len(descripcion) > 100:
        flash('El nombre de la marca admite máximo 100 caracteres.', 'error')
        return None

    # La BD compara sin distinguir mayúsculas: "apple" = "Apple".
    cursor.execute(
        "SELECT Descripcion FROM Marca WHERE Descripcion = ? AND IdMarca <> ?",
        descripcion, id_marca or 0,
    )
    duplicada = cursor.fetchone()
    if duplicada:
        flash(f'Ya existe la marca "{duplicada.Descripcion}".', 'error')
        return None
    return descripcion


# ==========================================================================
# PRODUCTOS
# ==========================================================================
@admin_bp.route('/productos')
@acceso_requerido
def lista_productos():
    productos = consultar_pagina(
        """
        SELECT p.IdProducto, p.Nombre, c.Descripcion AS Categoria, m.Descripcion AS Marca,
               p.Precio, p.Stock, p.StockMinimo, p.Activo, p.RutaImagen
        FROM Producto p
        LEFT JOIN Categoria c ON c.IdCategoria = p.IdCategoria
        LEFT JOIN Marca m ON m.IdMarca = p.IdMarca
        """,
        orden='p.Nombre, p.IdProducto',
    )
    return render_template('admin/productos.html', productos=productos)


@admin_bp.route('/productos/nuevo', methods=['GET', 'POST'])
@acceso_requerido
def nuevo_producto():
    categorias, marcas = _obtener_categorias_marcas_activas()

    if request.method == 'POST':
        datos = _leer_formulario_producto(categorias, marcas)
        if datos is None:
            return render_template('admin/producto_form.html', producto=_producto_desde_formulario(),
                                   es_nuevo=True, categorias=categorias, marcas=marcas)

        ruta_imagen, nombre_imagen, error_imagen = _guardar_imagen(request.files.get('imagen'))
        if error_imagen:
            flash(error_imagen, 'error')
            return render_template('admin/producto_form.html', producto=_producto_desde_formulario(),
                                   es_nuevo=True, categorias=categorias, marcas=marcas)

        conexion = conexion_actual()
        cursor = conexion.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO Producto (Nombre, Descripcion, IdCategoria, IdMarca, Precio,
                                       Stock, StockMinimo, RutaImagen, NombreImagen, Activo, IdUsuario)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                datos['nombre'], datos['descripcion'], datos['id_categoria'], datos['id_marca'],
                datos['precio'], datos['stock'], datos['stock_minimo'],
                ruta_imagen, nombre_imagen, session['id_usuario'],
            )
            conexion.commit()
        except Exception:
            conexion.rollback()
            _borrar_imagen(ruta_imagen, nombre_imagen)  # el producto no se creó: la foto subida sobra
            current_app.logger.exception('Error al crear el producto %s', datos['nombre'])
            flash('No se pudo crear el producto. Inténtalo nuevamente.', 'error')
            return render_template('admin/producto_form.html', producto=_producto_desde_formulario(),
                                   es_nuevo=True, categorias=categorias, marcas=marcas)
        flash('Producto creado correctamente.', 'success')
        return redirect(url_for('admin.lista_productos'))

    return render_template('admin/producto_form.html', producto=None, es_nuevo=True,
                           categorias=categorias, marcas=marcas)


@admin_bp.route('/productos/editar/<int:id_producto>', methods=['GET', 'POST'])
@acceso_requerido
def editar_producto(id_producto):
    categorias, marcas = _obtener_categorias_marcas_activas()
    conexion = conexion_actual()
    cursor = conexion.cursor()
    cursor.execute(
        """
        SELECT IdProducto, Nombre, Descripcion, IdCategoria, IdMarca,
               Precio, Stock, StockMinimo, Activo, RutaImagen, NombreImagen
        FROM Producto WHERE IdProducto = ?
        """,
        id_producto,
    )
    producto = cursor.fetchone()
    if producto is None:
        flash('El producto no existe.', 'error')
        return redirect(url_for('admin.lista_productos'))

    if request.method == 'POST':
        datos = _leer_formulario_producto(categorias, marcas)
        if datos is None:
            # Se vuelve a mostrar lo que el usuario escribió, para que solo corrija el campo con error.
            return render_template('admin/producto_form.html', producto=_producto_desde_formulario(producto),
                                   es_nuevo=False, categorias=categorias, marcas=marcas)

        # Se conserva la imagen actual si no suben una nueva
        ruta_imagen, nombre_imagen = producto.RutaImagen, producto.NombreImagen
        imagen_reemplazada = False

        archivo_subido = request.files.get('imagen')
        if archivo_subido and archivo_subido.filename:
            ruta_nueva, nombre_nuevo, error_imagen = _guardar_imagen(archivo_subido)
            if error_imagen:
                flash(error_imagen, 'error')
                return render_template('admin/producto_form.html', producto=_producto_desde_formulario(producto),
                                       es_nuevo=False, categorias=categorias, marcas=marcas)
            ruta_imagen, nombre_imagen = ruta_nueva, nombre_nuevo
            imagen_reemplazada = True

        activo = 1 if request.form.get('activo') == 'on' else 0
        try:
            cursor.execute(
                """
                UPDATE Producto
                SET Nombre = ?, Descripcion = ?, IdCategoria = ?, IdMarca = ?,
                    Precio = ?, Stock = ?, StockMinimo = ?, RutaImagen = ?, NombreImagen = ?, Activo = ?
                WHERE IdProducto = ?
                """,
                datos['nombre'], datos['descripcion'], datos['id_categoria'], datos['id_marca'],
                datos['precio'], datos['stock'], datos['stock_minimo'],
                ruta_imagen, nombre_imagen, activo, id_producto,
            )
            conexion.commit()
        except Exception:
            conexion.rollback()
            if imagen_reemplazada:
                _borrar_imagen(ruta_imagen, nombre_imagen)  # no se guardó: se descarta la foto nueva y queda la anterior
            current_app.logger.exception('Error al editar el producto %s', id_producto)
            flash('No se pudo actualizar el producto. Inténtalo nuevamente.', 'error')
            return render_template('admin/producto_form.html', producto=_producto_desde_formulario(producto),
                                   es_nuevo=False, categorias=categorias, marcas=marcas)

        # HU-09: la foto anterior se borra del servidor SOLO cuando la nueva ya quedó
        # guardada en la BD (si el UPDATE fallara, el producto seguiría usando la anterior).
        if imagen_reemplazada:
            _borrar_imagen(producto.RutaImagen, producto.NombreImagen)
        flash('Producto actualizado.', 'success')
        return redirect(url_for('admin.lista_productos'))

    return render_template('admin/producto_form.html', producto=producto, es_nuevo=False,
                           categorias=categorias, marcas=marcas)


def _producto_desde_formulario(producto_guardado=None):
    """Arma un 'producto' con lo enviado, para volver a mostrar el formulario si hubo un error."""
    f = request.form
    return SimpleNamespace(
        Nombre=f.get('nombre', ''), Descripcion=f.get('descripcion', ''),
        IdCategoria=int(f['id_categoria']) if f.get('id_categoria', '').isdigit() else None,
        IdMarca=int(f['id_marca']) if f.get('id_marca', '').isdigit() else None,
        Precio=f.get('precio', ''), Stock=f.get('stock', ''), StockMinimo=f.get('stock_minimo', ''),
        Activo=f.get('activo') == 'on',
        RutaImagen=producto_guardado.RutaImagen if producto_guardado else None,
    )


def _obtener_categorias_marcas_activas():
    """Trae categorías y marcas activas, para llenar los <select> del formulario."""
    conexion = conexion_actual()
    cursor = conexion.cursor()
    cursor.execute("SELECT IdCategoria, Descripcion FROM Categoria WHERE Activo = 1 ORDER BY Descripcion")
    categorias = cursor.fetchall()
    cursor.execute("SELECT IdMarca, Descripcion FROM Marca WHERE Activo = 1 ORDER BY Descripcion")
    marcas = cursor.fetchall()
    return categorias, marcas


def _leer_formulario_producto(categorias, marcas):
    """
    Lee y valida los campos del formulario de producto. Devuelve None si falta algo obligatorio.
    categorias/marcas: las activas que ofrece el formulario; solo esas se aceptan.
    """
    # El HTML ya restringe estos datos, pero se vuelven a validar aquí porque el
    # navegador se puede saltar (formulario editado a mano, petición directa, etc.).
    nombre = ' '.join(request.form.get('nombre', '').split())
    descripcion = request.form.get('descripcion', '').strip() or None
    id_categoria = request.form.get('id_categoria', '').strip()
    id_marca = request.form.get('id_marca', '').strip()
    precio = request.form.get('precio', '').strip()
    stock = request.form.get('stock', '').strip()
    stock_minimo = request.form.get('stock_minimo', '').strip()

    if not nombre:
        flash('El nombre del producto es obligatorio.', 'error')
        return None
    if len(nombre) > 500:
        flash('El nombre admite máximo 500 caracteres.', 'error')
        return None
    if descripcion and len(descripcion) > 500:
        flash('La descripción admite máximo 500 caracteres.', 'error')
        return None

    # HU-08: el producto se registra CON categoría y marca, y solo se aceptan las
    # activas que ofrece el formulario (una anulada o inexistente se rechaza).
    if not id_categoria.isdigit() or int(id_categoria) not in {c.IdCategoria for c in categorias}:
        flash('Selecciona una categoría válida (activa).', 'error')
        return None
    if not id_marca.isdigit() or int(id_marca) not in {m.IdMarca for m in marcas}:
        flash('Selecciona una marca válida (activa).', 'error')
        return None

    # Precio: columna DECIMAL(10,2) -> hasta 8 enteros y 2 decimales, mayor que cero.
    # (float() aceptaría "nan", "inf", "-5" o "1e3"; por eso se valida el texto exacto.)
    if not re.fullmatch(r'\d{1,8}(\.\d{1,2})?', precio) or Decimal(precio) <= 0:
        flash('El precio debe ser un número mayor que 0, con hasta 2 decimales (máximo 99999999.99).', 'error')
        return None

    # Stock y stock mínimo: enteros, cero o más (columnas INT).
    if not re.fullmatch(r'\d{1,9}', stock):
        flash('El stock debe ser un número entero, cero o mayor.', 'error')
        return None
    if not re.fullmatch(r'\d{1,9}', stock_minimo):
        flash('El stock mínimo (umbral de bajo stock) debe ser un número entero, cero o mayor.', 'error')
        return None

    precio, stock, stock_minimo = Decimal(precio), int(stock), int(stock_minimo)
    id_categoria, id_marca = int(id_categoria), int(id_marca)

    return {
        'nombre': nombre,
        'descripcion': descripcion,
        'id_categoria': id_categoria,
        'id_marca': id_marca,
        'precio': precio,
        'stock': stock,
        'stock_minimo': stock_minimo,
    }


def _guardar_imagen(archivo):
    """
    Sube la foto del producto a la carpeta de fotos del hosting (almacen_fotos).
    NUNCA se guarda el binario en la base de datos — solo se devuelve la dirección
    pública (para el <img>) y el nombre real del archivo (para poder borrarlo
    después si se reemplaza).

    Devuelve (ruta_imagen, nombre_imagen, error). Si no se subió ningún
    archivo, devuelve (None, None, None) y el producto queda sin foto.
    """
    if archivo is None or archivo.filename == '':
        return None, None, None

    extension = archivo.filename.rsplit('.', 1)[-1].lower() if '.' in archivo.filename else ''
    if extension not in EXTENSIONES_PERMITIDAS:
        return None, None, (f'El archivo "{archivo.filename}" no está permitido. '
                            'Solo se aceptan imágenes JPG, JPEG, PNG, GIF o WEBP.')

    archivo.stream.seek(0, os.SEEK_END)
    tamano = archivo.stream.tell()
    archivo.stream.seek(0)
    if tamano > TAMANO_MAXIMO_IMAGEN_MB * 1024 * 1024:
        return None, None, (f'El archivo "{archivo.filename}" pesa {tamano / 1024 / 1024:.1f} MB. '
                            f'El tamaño máximo permitido es {TAMANO_MAXIMO_IMAGEN_MB} MB.')
    if tamano == 0:
        return None, None, f'El archivo "{archivo.filename}" está vacío.'

    # La extensión se puede cambiar a mano (ej. "programa.exe" renombrado a "foto.jpg"):
    # se revisa que el contenido sea realmente una imagen de ese formato.
    cabecera = archivo.stream.read(12)
    archivo.stream.seek(0)
    if not _es_imagen_del_formato(cabecera, extension):
        return None, None, (f'El archivo "{archivo.filename}" no es una imagen {extension.upper()} válida. '
                            'Solo se aceptan imágenes JPG, JPEG, PNG, GIF o WEBP.')

    # Nombre único: dos productos podrían subir una foto llamada igual
    # (por ejemplo "foto.jpg"), y no queremos que una se sobreescriba a la otra.
    nombre_archivo = f"{uuid.uuid4().hex}.{extension}"
    nombre_archivo = secure_filename(nombre_archivo)

    try:
        ruta_publica = almacen_fotos.subir(nombre_archivo, archivo.stream)
    except almacen_fotos.ErrorAlmacenFotos as error:
        current_app.logger.exception('No se pudo subir la foto %s al hosting', nombre_archivo)
        return None, None, f'No se pudo guardar la foto: {error}'
    return ruta_publica, nombre_archivo, None


def _es_imagen_del_formato(cabecera, extension):
    """Compara los primeros bytes del archivo con la firma conocida de cada formato."""
    if extension in ('jpg', 'jpeg'):
        return cabecera.startswith(b'\xff\xd8\xff')
    if extension == 'png':
        return cabecera.startswith(b'\x89PNG\r\n\x1a\n')
    if extension == 'gif':
        return cabecera[:6] in (b'GIF87a', b'GIF89a')
    if extension == 'webp':
        return cabecera[:4] == b'RIFF' and cabecera[8:12] == b'WEBP'
    return False


def _borrar_imagen(ruta_imagen, nombre_imagen):
    """
    Borra la foto que ya no se usa (reemplazada, o subida para un producto que no se guardó).
    Si está en el hosting se borra allí; las fotos antiguas guardadas en la PC
    (static/uploads/productos) se borran de la PC.
    """
    if not nombre_imagen:
        return
    if almacen_fotos.es_del_hosting(ruta_imagen):
        almacen_fotos.borrar(nombre_imagen)  # si falla, no es crítico: no rompe el flujo del producto
        return
    ruta_absoluta = os.path.join(current_app.root_path, CARPETA_PRODUCTOS, nombre_imagen)
    try:
        if os.path.isfile(ruta_absoluta):
            os.remove(ruta_absoluta)
    except OSError:
        pass  # si no se pudo borrar, no es crítico: no rompe el flujo del producto
