"""
Módulo de seguridad para el PERSONAL INTERNO (Administrador y Operador).
Comparten una misma pantalla de login con un selector de perfil,
pero el sistema SIEMPRE valida en la base de datos que el usuario
realmente tenga asignado el perfil que seleccionó — nunca se confía
en lo que el formulario indique.

Las contraseñas se guardan con HASHBYTES('SHA2_256', clave + salt) nativo
de SQL Server (columna Clave VARBINARY(32) + columna Salt VARCHAR(32)),
en vez de un hash largo generado en Python — pedido del profesor para
ahorrar espacio en disco.

El Cliente sigue teniendo su propio login, completamente separado
(ver routes/cliente.py).
"""
import re
from functools import wraps
from datetime import datetime
import pyodbc
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, current_app
from db import conexion_actual, consultar_todos, resultado_procedimiento
from paginacion import consultar_pagina
from seguridad import SQL_HASH_CLAVE, validar_clave_nueva, clave_ingresada_es_comparable, generar_salt_unica

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')

# Los perfiles se identifican por su IdPerfil (tabla Perfiles), NUNCA por su nombre:
# así el Administrador puede renombrarlos (ej. "Super Administrador") o cambiar su
# descripción sin que nadie pierda el acceso — el cambio se refleja para todos.
ID_PERFIL_ADMINISTRADOR = 1
ID_PERFIL_CLIENTE = 2

# Perfiles de personal interno = TODOS los perfiles salvo Cliente. Así, un perfil
# nuevo creado en "Perfiles" queda disponible para asignarse a usuarios y para
# iniciar sesión con él (HU-02). El Cliente NUNCA entra por este panel: tiene
# su propia tabla y su propio login (routes/cliente.py).
def es_perfil_interno(id_perfil):
    return isinstance(id_perfil, int) and id_perfil != ID_PERFIL_CLIENTE


# URL de la opción "Asignar Accesos": el perfil que la usa no puede quitársela
# a sí mismo, o nadie podría volver a dar permisos desde el sistema.
URL_ASIGNAR_ACCESOS = '/admin/accesos/asignar'


def _normalizar_url(url):
    return (url or '').strip().rstrip('/').lower()


def _verificar_sesion():
    """
    Revisa en la base de datos, en cada clic:
      1. Que el usuario siga activo y conserve el perfil con el que entró
         (si lo anulan o le quitan el perfil, queda fuera en su siguiente clic).
      2. Qué URLs de menú tiene asignadas hoy su perfil (OpcionesMenu_Perfiles):
         esas asignaciones son la ÚNICA fuente de permisos del panel (HU3).
    De paso refresca el nombre del perfil en la sesión (si fue renombrado).
    Devuelve la lista de URLs permitidas, o None si la sesión ya no es válida.
    """
    if not es_perfil_interno(session.get('id_perfil')):
        return None

    conexion = conexion_actual()
    cursor = conexion.cursor()
    cursor.execute(
        """
        SELECT p.Nombre
        FROM Usuario u
        INNER JOIN Usuario_Perfiles up ON up.IdUsuario = u.IdUsuario
        INNER JOIN Perfiles p ON p.IdPerfil = up.IdPerfil
        WHERE u.IdUsuario = ? AND up.IdPerfil = ?
              AND u.EstadoRegistro = 1 AND up.EstadoRegistro = 1 AND p.EstadoRegistro = 1
        """,
        session.get('id_usuario'), session.get('id_perfil'),
    )
    fila = cursor.fetchone()
    if fila is None:
        return None
    session['nombre_perfil'] = fila.Nombre

    cursor.execute(
        """
        SELECT om.UrlMenu
        FROM OpcionesMenu_Perfiles omp
        INNER JOIN OpcionesMenu om ON om.IdOpcionMenu = omp.IdOpcionMenu
        WHERE omp.IdPerfil = ? AND omp.EstadoRegistro = 1 AND om.EstadoRegistro = 1
        """,
        session['id_perfil'],
    )
    urls_permitidas = [_normalizar_url(f.UrlMenu) for f in cursor.fetchall() if f.UrlMenu]
    return urls_permitidas


def _ruta_permitida(ruta, urls_permitidas):
    """
    Una ruta está permitida si es exactamente una URL asignada o una subpágina suya:
    tener '/admin/proveedores' habilita también '/admin/proveedores/nuevo' y
    '/admin/proveedores/editar/5', pero NO '/admin/proveedoresX'.
    """
    ruta = _normalizar_url(ruta)
    return any(ruta == url or ruta.startswith(url + '/') for url in urls_permitidas)


def _expulsar():
    """Cierra la sesión de un usuario anulado (o sin sesión) y lo manda al login."""
    habia_sesion = 'id_usuario' in session
    session.clear()
    if habia_sesion:
        flash('Tu usuario fue anulado o ya no tiene acceso con ese perfil.', 'error')
    return redirect(url_for('admin.login'))


def sesion_requerida(vista):
    """Solo exige una sesión válida de personal interno (para el inicio del panel, que todos ven)."""
    @wraps(vista)
    def envoltura(*args, **kwargs):
        if _verificar_sesion() is None:
            return _expulsar()
        return vista(*args, **kwargs)
    return envoltura


def acceso_requerido(vista):
    """
    Protege una vista según las opciones de menú asignadas al perfil (Asignar Accesos).
    Si el usuario escribe la URL directamente sin tenerla asignada, se le niega el acceso,
    aunque sea Administrador.
    """
    @wraps(vista)
    def envoltura(*args, **kwargs):
        urls_permitidas = _verificar_sesion()
        if urls_permitidas is None:
            return _expulsar()
        if not _ruta_permitida(request.path, urls_permitidas):
            flash('No tienes acceso a esa opción. Pide al Administrador que se la asigne a tu perfil.', 'error')
            return redirect(url_for('admin.panel'))
        return vista(*args, **kwargs)
    return envoltura


@admin_bp.route('/login', methods=['GET', 'POST'])
def login():
    perfiles_disponibles = _obtener_perfiles_personal_interno()

    if request.method == 'POST':
        correo = request.form.get('correo', '').strip()
        clave_ingresada = request.form.get('clave', '')
        id_perfil_elegido = request.form.get('id_perfil', '')

        if not id_perfil_elegido.isdigit():
            flash('Selecciona un perfil de acceso.', 'error')
            return render_template('admin/login.html', perfiles=perfiles_disponibles)
        id_perfil_elegido = int(id_perfil_elegido)

        # Una clave que nunca podría compararse de forma segura (muy larga o con
        # caracteres que SQL Server volvería '?') se rechaza sin consultar la BD.
        if not correo or not clave_ingresada_es_comparable(clave_ingresada):
            flash('Correo o contraseña incorrectos.', 'error')
            return render_template('admin/login.html', perfiles=perfiles_disponibles)

        # La comparación del hash se hace en SQL Server con HASHBYTES, usando la
        # salt guardada de este usuario: Python nunca ve ni recibe el hash real,
        # solo pregunta "¿coincide, sí o no?". La contraseña no se imprime ni se
        # registra en ningún log, y la conexión con la BD va cifrada (db.py).
        # Procedimiento almacenado usp_Usuario_Login (sql/stored_procedures.sql).
        conexion = conexion_actual()
        cursor = conexion.cursor()
        cursor.execute("EXEC dbo.usp_Usuario_Login @Correo = ?, @Clave = ?", correo, clave_ingresada)
        fila = resultado_procedimiento(cursor)

        # Mismo mensaje si el correo no existe o si la clave es incorrecta:
        # así no se puede averiguar qué correos están registrados.
        if fila is None:
            flash('Correo o contraseña incorrectos.', 'error')
            return render_template('admin/login.html', perfiles=perfiles_disponibles)

        id_usuario, nombres, apellido_paterno, estado_registro = fila

        # El estado se revela solo a quien ya demostró conocer la contraseña.
        if not estado_registro:
            flash('Este usuario está anulado. Comunícate con el Administrador.', 'error')
            return render_template('admin/login.html', perfiles=perfiles_disponibles)

        # Validación clave: ¿el usuario REALMENTE tiene asignado el perfil elegido,
        # Y ese perfil es de personal interno (Administrador/Operador)?
        cursor.execute(
            """
            SELECT p.Nombre
            FROM Usuario_Perfiles up
            INNER JOIN Perfiles p ON p.IdPerfil = up.IdPerfil
            WHERE up.IdUsuario = ? AND up.IdPerfil = ?
                  AND up.EstadoRegistro = 1 AND p.EstadoRegistro = 1
            """,
            id_usuario, id_perfil_elegido,
        )
        perfil_validado = cursor.fetchone()

        if perfil_validado is None or not es_perfil_interno(id_perfil_elegido):
            flash('No tienes acceso con el perfil seleccionado.', 'error')
            return render_template('admin/login.html', perfiles=perfiles_disponibles)

        nombre_perfil = perfil_validado[0]

        session.clear()
        session['id_usuario'] = id_usuario
        session['nombre_completo'] = f"{nombres} {apellido_paterno}"
        session['id_perfil'] = id_perfil_elegido
        session['nombre_perfil'] = nombre_perfil
        _cargar_menu_por_perfil(id_perfil_elegido)

        return redirect(url_for('admin.panel'))

    return render_template('admin/login.html', perfiles=perfiles_disponibles)


def _obtener_perfiles_personal_interno():
    """Perfiles activos de personal interno para el selector del login (nunca Cliente), con su nombre actual."""
    conexion = conexion_actual()
    cursor = conexion.cursor()
    cursor.execute(
        "SELECT IdPerfil, Nombre FROM Perfiles WHERE EstadoRegistro = 1 AND IdPerfil <> ? ORDER BY Nombre",
        ID_PERFIL_CLIENTE,
    )
    perfiles = [{'id': fila.IdPerfil, 'nombre': fila.Nombre} for fila in cursor.fetchall()]
    return perfiles


def _cargar_menu_por_perfil(id_perfil):
    """
    Carga en sesión el menú lateral del perfil YA VALIDADO, con submenús (HU-03):
    cada opción hija asignada aparece debajo de su padre. Si la hija está asignada
    pero su padre no (o el padre está inactivo), la hija se muestra en el nivel
    superior: así nunca queda una opción asignada que no se vea.
    """
    filas = consultar_todos(
        """
        SELECT om.IdOpcionMenu, om.Nombre, om.UrlMenu, om.IdPadre
        FROM OpcionesMenu_Perfiles omp
        INNER JOIN OpcionesMenu om ON om.IdOpcionMenu = omp.IdOpcionMenu
        WHERE omp.IdPerfil = ? AND omp.EstadoRegistro = 1 AND om.EstadoRegistro = 1
        ORDER BY omp.Orden, om.Nombre
        """,
        id_perfil,
    )

    opciones = {f.IdOpcionMenu: {'id': f.IdOpcionMenu, 'nombre': f.Nombre, 'url': f.UrlMenu, 'hijas': []}
                for f in filas}
    menu = []
    for f in filas:
        if f.IdPadre in opciones:
            opciones[f.IdPadre]['hijas'].append(opciones[f.IdOpcionMenu])
        else:
            menu.append(opciones[f.IdOpcionMenu])
    session['menu'] = menu


@admin_bp.route('/')
@sesion_requerida
def panel():
    return render_template('admin/panel.html')


@admin_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('admin.login'))


# ==========================================================================
# USUARIOS (acceso según la opción de menú asignada al perfil)
# ==========================================================================
@admin_bp.route('/usuarios')
@acceso_requerido
def lista_usuarios():
    usuarios = consultar_pagina(
        """
        SELECT u.IdUsuario, u.Nombres, u.ApellidoPaterno, u.CorreoElectronico,
               u.EstadoRegistro, STRING_AGG(p.Nombre, ', ') AS Perfiles
        FROM Usuario u
        LEFT JOIN Usuario_Perfiles up ON up.IdUsuario = u.IdUsuario AND up.EstadoRegistro = 1
        LEFT JOIN Perfiles p ON p.IdPerfil = up.IdPerfil
        GROUP BY u.IdUsuario, u.Nombres, u.ApellidoPaterno, u.CorreoElectronico, u.EstadoRegistro
        """,
        orden='u.Nombres, u.IdUsuario',
    )
    return render_template('admin/usuarios.html', usuarios=usuarios)


@admin_bp.route('/usuarios/nuevo', methods=['GET', 'POST'])
@acceso_requerido
def nuevo_usuario():
    conexion = conexion_actual()
    cursor = conexion.cursor()
    perfiles = _obtener_perfiles_activos(cursor)

    if request.method == 'POST':
        datos = _leer_formulario_usuario(cursor, perfiles, id_usuario=None)
        if datos is None:
            return render_template('admin/usuario_form.html', perfiles=perfiles, usuario=None,
                                   perfiles_usuario=_perfiles_marcados())

        try:
            # Salt única de 32 caracteres; el hash real lo calcula SQL Server con HASHBYTES, nunca Python.
            salt = generar_salt_unica(cursor)
            cursor.execute(
                f"""
                INSERT INTO Usuario (DNI, Nombres, ApellidoPaterno, ApellidoMaterno, Celular,
                                      CorreoElectronico, Clave, Salt, UsuarioCreacion, FechaCreacion, EstadoRegistro)
                OUTPUT INSERTED.IdUsuario
                VALUES (?, ?, ?, ?, ?, ?, {SQL_HASH_CLAVE}, ?, ?, ?, 1)
                """,
                datos['dni'], datos['nombres'], datos['apellido_paterno'], datos['apellido_materno'],
                datos['celular'], datos['correo'], datos['clave'], salt, salt,
                session['id_usuario'], datetime.now(),
            )
            id_usuario_nuevo = cursor.fetchone()[0]
            _guardar_perfiles_usuario(cursor, id_usuario_nuevo, datos['ids_perfiles'])
            conexion.commit()
            flash('Usuario creado correctamente.', 'success')
            return redirect(url_for('admin.lista_usuarios'))
        except pyodbc.IntegrityError:
            # Otro usuario guardó el mismo correo o DNI justo antes (índices únicos de la BD).
            conexion.rollback()
            flash('El correo o el DNI ya está registrado para otro usuario.', 'error')
            return render_template('admin/usuario_form.html', perfiles=perfiles, usuario=None,
                                   perfiles_usuario=_perfiles_marcados())
        except Exception:
            conexion.rollback()
            # El detalle técnico va al log del servidor, nunca a la pantalla.
            current_app.logger.exception('Error al crear usuario con correo %s', datos['correo'])
            flash('No se pudo crear el usuario. Inténtalo nuevamente.', 'error')
            return render_template('admin/usuario_form.html', perfiles=perfiles, usuario=None,
                                   perfiles_usuario=_perfiles_marcados())

    return render_template('admin/usuario_form.html', perfiles=perfiles, usuario=None, perfiles_usuario=[])


@admin_bp.route('/usuarios/editar/<int:id_usuario>', methods=['GET', 'POST'])
@acceso_requerido
def editar_usuario(id_usuario):
    conexion = conexion_actual()
    cursor = conexion.cursor()
    perfiles = _obtener_perfiles_activos(cursor)

    cursor.execute(
        """
        SELECT IdUsuario, DNI, Nombres, ApellidoPaterno, ApellidoMaterno, Celular,
               CorreoElectronico, EstadoRegistro
        FROM Usuario WHERE IdUsuario = ?
        """,
        id_usuario,
    )
    usuario = cursor.fetchone()
    if usuario is None:
        flash('El usuario no existe.', 'error')
        return redirect(url_for('admin.lista_usuarios'))

    if request.method == 'POST':
        datos = _leer_formulario_usuario(cursor, perfiles, id_usuario=id_usuario)

        # Protección contra quedarse fuera: el Administrador no puede quitarse a sí
        # mismo el perfil con el que tiene la sesión abierta.
        if datos and id_usuario == session['id_usuario'] and session['id_perfil'] not in datos['ids_perfiles']:
            flash('No puedes quitarte el perfil con el que tienes la sesión abierta.', 'error')
            datos = None

        if datos is None:
            return render_template('admin/usuario_form.html', perfiles=perfiles, usuario=usuario,
                                   perfiles_usuario=_perfiles_marcados())

        try:
            cursor.execute(
                """
                UPDATE Usuario
                SET DNI = ?, Nombres = ?, ApellidoPaterno = ?, ApellidoMaterno = ?, Celular = ?,
                    CorreoElectronico = ?, UsuarioModificacion = ?, FechaModificacion = ?
                WHERE IdUsuario = ?
                """,
                datos['dni'], datos['nombres'], datos['apellido_paterno'], datos['apellido_materno'],
                datos['celular'], datos['correo'], session['id_usuario'], datetime.now(), id_usuario,
            )
            # La contraseña solo cambia si se escribió una nueva; vacío = se mantiene la actual.
            # Al cambiarla se genera también una salt nueva.
            if datos['clave']:
                salt = generar_salt_unica(cursor)
                cursor.execute(
                    f"""
                    UPDATE Usuario
                    SET Clave = {SQL_HASH_CLAVE}, Salt = ?
                    WHERE IdUsuario = ?
                    """,
                    datos['clave'], salt, salt, id_usuario,
                )
            _guardar_perfiles_usuario(cursor, id_usuario, datos['ids_perfiles'])
            conexion.commit()
            flash('Usuario actualizado correctamente.', 'success')
            return redirect(url_for('admin.lista_usuarios'))
        except pyodbc.IntegrityError:
            conexion.rollback()
            flash('El correo o el DNI ya está registrado para otro usuario.', 'error')
            return render_template('admin/usuario_form.html', perfiles=perfiles, usuario=usuario,
                                   perfiles_usuario=_perfiles_marcados())
        except Exception:
            conexion.rollback()
            current_app.logger.exception('Error al editar el usuario %s', id_usuario)
            flash('No se pudo actualizar el usuario. Inténtalo nuevamente.', 'error')
            return render_template('admin/usuario_form.html', perfiles=perfiles, usuario=usuario,
                                   perfiles_usuario=_perfiles_marcados())

    cursor.execute(
        "SELECT IdPerfil FROM Usuario_Perfiles WHERE IdUsuario = ? AND EstadoRegistro = 1",
        id_usuario,
    )
    perfiles_usuario = [fila.IdPerfil for fila in cursor.fetchall()]
    return render_template('admin/usuario_form.html', perfiles=perfiles, usuario=usuario,
                           perfiles_usuario=perfiles_usuario)


@admin_bp.route('/usuarios/estado/<int:id_usuario>', methods=['POST'])
@acceso_requerido
def cambiar_estado_usuario(id_usuario):
    """Anula (EstadoRegistro = 0) o reactiva (1) un usuario. Nunca se borra: queda el historial."""
    if id_usuario == session['id_usuario']:
        flash('No puedes anular tu propio usuario.', 'error')
        return redirect(url_for('admin.lista_usuarios'))

    nuevo_estado = 1 if request.form.get('accion') == 'reactivar' else 0
    conexion = conexion_actual()
    cursor = conexion.cursor()
    cursor.execute(
        """
        UPDATE Usuario
        SET EstadoRegistro = ?, UsuarioModificacion = ?, FechaModificacion = ?
        WHERE IdUsuario = ?
        """,
        nuevo_estado, session['id_usuario'], datetime.now(), id_usuario,
    )
    conexion.commit()
    flash('Usuario reactivado.' if nuevo_estado else 'Usuario anulado. Ya no podrá ingresar al sistema.', 'success')
    return redirect(url_for('admin.lista_usuarios'))


def _obtener_perfiles_activos(cursor):
    """
    Perfiles activos de personal interno (HU-02/HU-04): incluye los perfiles nuevos
    que cree el Administrador; un usuario interno nunca puede ser Cliente.
    """
    cursor.execute(
        "SELECT IdPerfil, Nombre FROM Perfiles WHERE EstadoRegistro = 1 AND IdPerfil <> ? ORDER BY Nombre",
        ID_PERFIL_CLIENTE,
    )
    return [{'id': fila.IdPerfil, 'nombre': fila.Nombre} for fila in cursor.fetchall()]


def _perfiles_marcados():
    """Perfiles marcados en el formulario enviado (para no perderlos si hay que volver a mostrarlo)."""
    return [int(i) for i in request.form.getlist('id_perfil') if i.isdigit()]


def _leer_formulario_usuario(cursor, perfiles, id_usuario):
    """
    Lee y valida el formulario de usuario. id_usuario=None significa alta nueva
    (contraseña obligatoria); en edición la contraseña es opcional.
    Devuelve un dict con los datos, o None si algo no es válido (ya con el flash puesto).
    """
    datos = {
        'dni': request.form.get('dni', '').strip(),
        'nombres': request.form.get('nombres', '').strip(),
        'apellido_paterno': request.form.get('apellido_paterno', '').strip(),
        'apellido_materno': request.form.get('apellido_materno', '').strip() or None,
        'celular': request.form.get('celular', '').strip() or None,
        'correo': request.form.get('correo', '').strip(),
        'clave': request.form.get('clave', ''),
    }

    if not all((datos['dni'], datos['nombres'], datos['apellido_paterno'], datos['correo'])):
        flash('Completa todos los campos obligatorios.', 'error')
        return None

    # El HTML ya restringe el formato, pero se vuelve a validar aquí porque el
    # navegador se puede saltar (formulario editado a mano, petición directa, etc.).
    if not re.fullmatch(r'\d{8}', datos['dni']):
        flash('El DNI debe tener exactamente 8 dígitos numéricos.', 'error')
        return None

    if datos['celular'] and not re.fullmatch(r'\d{1,12}', datos['celular']):
        flash('El celular solo puede contener números, hasta 12 dígitos.', 'error')
        return None

    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', datos['correo']) or len(datos['correo']) > 40:
        flash('Ingresa un correo electrónico válido (máximo 40 caracteres).', 'error')
        return None

    if max(len(datos['nombres']), len(datos['apellido_paterno']), len(datos['apellido_materno'] or '')) > 40:
        flash('Nombres y apellidos admiten máximo 40 caracteres.', 'error')
        return None

    # Contraseña: obligatoria al crear; al editar, vacía = se mantiene la actual.
    if id_usuario is None or datos['clave']:
        error = validar_clave_nueva(datos['clave'], request.form.get('clave_confirmar', ''))
        if error:
            flash(error, 'error')
            return None

    # Solo se aceptan perfiles que realmente existan y estén activos.
    ids_validos = {p['id'] for p in perfiles}
    datos['ids_perfiles'] = [i for i in _perfiles_marcados() if i in ids_validos]
    if not datos['ids_perfiles']:
        flash('Selecciona al menos un perfil.', 'error')
        return None

    # La tabla no tiene restricción UNIQUE, así que se valida aquí (excluyendo al propio usuario al editar).
    cursor.execute(
        "SELECT CorreoElectronico, DNI FROM Usuario WHERE (CorreoElectronico = ? OR DNI = ?) AND IdUsuario <> ?",
        datos['correo'], datos['dni'], id_usuario or 0,
    )
    duplicado = cursor.fetchone()
    if duplicado:
        # La BD compara sin distinguir mayúsculas (collation CI): aquí también.
        campo = 'correo' if duplicado.CorreoElectronico.lower() == datos['correo'].lower() else 'DNI'
        flash(f'Ya existe otro usuario con ese {campo}.', 'error')
        return None

    return datos


def _guardar_perfiles_usuario(cursor, id_usuario, ids_perfiles):
    """
    Deja activos exactamente los perfiles indicados. Los que se quitan no se
    borran: quedan con EstadoRegistro = 0 (mismo criterio que el resto del sistema).
    """
    cursor.execute("UPDATE Usuario_Perfiles SET EstadoRegistro = 0 WHERE IdUsuario = ?", id_usuario)
    for id_perfil in ids_perfiles:
        cursor.execute(
            "UPDATE Usuario_Perfiles SET EstadoRegistro = 1 WHERE IdUsuario = ? AND IdPerfil = ?",
            id_usuario, id_perfil,
        )
        if cursor.rowcount == 0:
            cursor.execute(
                "INSERT INTO Usuario_Perfiles (IdUsuario, IdPerfil, EstadoRegistro) VALUES (?, ?, 1)",
                id_usuario, id_perfil,
            )
