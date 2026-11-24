"""
Módulo de ACCESOS: el corazón del control de permisos.
Permite gestionar los Perfiles, las Opciones de Menú, y decidir
qué opciones puede ver cada perfil (tabla OpcionesMenu_Perfiles).

Las asignaciones de OpcionesMenu_Perfiles son la ÚNICA fuente de permisos
del panel (acceso_requerido): definen tanto el menú como las URLs permitidas.
"""
import re
from flask import render_template, request, redirect, url_for, flash, session
from db import conexion_actual, consultar_todos
from routes.administrador import (admin_bp, acceso_requerido, ID_PERFIL_ADMINISTRADOR, ID_PERFIL_CLIENTE,
                                  URL_ASIGNAR_ACCESOS, _normalizar_url)


# ==========================================================================
# PERFILES
# ==========================================================================
@admin_bp.route('/accesos/perfiles')
@acceso_requerido
def lista_perfiles():
    """HU-02: perfiles de usuario INTERNO (el perfil Cliente no se gestiona aquí)."""
    perfiles = consultar_todos(
        """
        SELECT p.IdPerfil, p.Nombre, p.Descripcion, p.EstadoRegistro,
               (SELECT COUNT(*) FROM Usuario_Perfiles up INNER JOIN Usuario u ON u.IdUsuario = up.IdUsuario
                WHERE up.IdPerfil = p.IdPerfil AND up.EstadoRegistro = 1 AND u.EstadoRegistro = 1) AS UsuariosActivos
        FROM Perfiles p
        WHERE p.IdPerfil <> ?
        ORDER BY p.Nombre
        """,
        ID_PERFIL_CLIENTE,
    )
    return render_template('admin/accesos_perfiles.html', perfiles=perfiles)


@admin_bp.route('/accesos/perfiles/nuevo', methods=['GET', 'POST'])
@acceso_requerido
def nuevo_perfil():
    if request.method == 'POST':
        conexion = conexion_actual()
        cursor = conexion.cursor()
        datos = _leer_formulario_perfil(cursor, id_perfil=None)
        if datos is None:
            return render_template('admin/accesos_perfil_form.html', perfil=None)

        cursor.execute(
            "INSERT INTO Perfiles (Nombre, Descripcion, EstadoRegistro) VALUES (?, ?, 1)",
            datos['nombre'], datos['descripcion'],
        )
        conexion.commit()
        flash('Perfil creado correctamente. Ya puedes asignarlo a usuarios y a opciones de menú.', 'success')
        return redirect(url_for('admin.lista_perfiles'))

    return render_template('admin/accesos_perfil_form.html', perfil=None)


@admin_bp.route('/accesos/perfiles/editar/<int:id_perfil>', methods=['GET', 'POST'])
@acceso_requerido
def editar_perfil(id_perfil):
    conexion = conexion_actual()
    cursor = conexion.cursor()
    cursor.execute("SELECT IdPerfil, Nombre, Descripcion, EstadoRegistro FROM Perfiles WHERE IdPerfil = ?", id_perfil)
    perfil = cursor.fetchone()
    if perfil is None or id_perfil == ID_PERFIL_CLIENTE:
        flash('El perfil no existe.', 'error')
        return redirect(url_for('admin.lista_perfiles'))

    if request.method == 'POST':
        datos = _leer_formulario_perfil(cursor, id_perfil=id_perfil)
        if datos is None:
            return render_template('admin/accesos_perfil_form.html', perfil=perfil)

        # Aquí solo se edita nombre y descripción. Como usuarios y menús guardan el
        # IdPerfil, el cambio se refleja para todos los que lo tienen asignado (HU-02).
        # Anular/reactivar se hace con los botones de la lista (cambiar_estado_perfil).
        cursor.execute(
            "UPDATE Perfiles SET Nombre = ?, Descripcion = ? WHERE IdPerfil = ?",
            datos['nombre'], datos['descripcion'], id_perfil,
        )
        conexion.commit()
        flash('Perfil actualizado. El cambio se refleja para todos sus usuarios.', 'success')
        return redirect(url_for('admin.lista_perfiles'))

    return render_template('admin/accesos_perfil_form.html', perfil=perfil)


def _leer_formulario_perfil(cursor, id_perfil):
    """
    Valida nombre (obligatorio, máx. 50, sin repetirse) y descripción (obligatoria,
    máx. 500), según HU-02 y el tamaño de las columnas de Perfiles.
    Devuelve el dict de datos, o None si algo no es válido (ya con el flash).
    """
    nombre = ' '.join(request.form.get('nombre', '').split())
    descripcion = ' '.join(request.form.get('descripcion', '').split())
    if not nombre:
        flash('El nombre del perfil es obligatorio.', 'error')
        return None
    if len(nombre) > 50:
        flash('El nombre del perfil admite máximo 50 caracteres.', 'error')
        return None
    if not descripcion:
        flash('La descripción del perfil es obligatoria.', 'error')
        return None
    if len(descripcion) > 500:
        flash('La descripción admite máximo 500 caracteres.', 'error')
        return None
    # Dos perfiles con el mismo nombre serían indistinguibles en el login (sin distinguir mayúsculas).
    cursor.execute("SELECT Nombre FROM Perfiles WHERE Nombre = ? AND IdPerfil <> ?", nombre, id_perfil or 0)
    duplicado = cursor.fetchone()
    if duplicado:
        flash(f'Ya existe el perfil "{duplicado.Nombre}".', 'error')
        return None
    return {'nombre': nombre, 'descripcion': descripcion}


@admin_bp.route('/accesos/perfiles/estado/<int:id_perfil>', methods=['POST'])
@acceso_requerido
def cambiar_estado_perfil(id_perfil):
    """Anula (EstadoRegistro = 0) o reactiva (1) un perfil. Nunca se borra."""
    reactivar = request.form.get('accion') == 'reactivar'
    conexion = conexion_actual()
    cursor = conexion.cursor()

    cursor.execute("SELECT Nombre FROM Perfiles WHERE IdPerfil = ?", id_perfil)
    perfil = cursor.fetchone()
    if perfil is None or id_perfil == ID_PERFIL_CLIENTE:
        flash('El perfil no existe.', 'error')
        return redirect(url_for('admin.lista_perfiles'))

    if not reactivar:
        # Desactivar el perfil Administrador dejaría a todos fuera de este panel.
        if id_perfil == ID_PERFIL_ADMINISTRADOR:
            flash(f'El perfil "{perfil.Nombre}" no se puede anular: nadie podría administrar el sistema.', 'error')
            return redirect(url_for('admin.lista_perfiles'))

        # HU-02: no se puede anular un perfil que tiene usuarios ACTIVOS asignados.
        cursor.execute(
            """
            SELECT COUNT(*) FROM Usuario_Perfiles up
            INNER JOIN Usuario u ON u.IdUsuario = up.IdUsuario
            WHERE up.IdPerfil = ? AND up.EstadoRegistro = 1 AND u.EstadoRegistro = 1
            """,
            id_perfil,
        )
        usuarios_activos = cursor.fetchone()[0]
        if usuarios_activos:
            flash(f'No se puede anular el perfil "{perfil.Nombre}" porque tiene {usuarios_activos} '
                  f'usuario(s) activo(s) asignado(s). Primero quítales este perfil desde Usuarios.', 'error')
            return redirect(url_for('admin.lista_perfiles'))

    cursor.execute(
        "UPDATE Perfiles SET EstadoRegistro = ? WHERE IdPerfil = ?",
        1 if reactivar else 0, id_perfil,
    )
    conexion.commit()
    flash(f'Perfil "{perfil.Nombre}" ' + ('reactivado.' if reactivar else 'anulado.'), 'success')
    return redirect(url_for('admin.lista_perfiles'))


# ==========================================================================
# OPCIONES DE MENÚ
# ==========================================================================
@admin_bp.route('/accesos/opciones-menu')
@acceso_requerido
def lista_opciones_menu():
    opciones = consultar_todos(
        """
        SELECT om.IdOpcionMenu, om.Nombre, om.UrlMenu, om.Descripcion,
               om.EstadoRegistro, padre.Nombre AS NombrePadre
        FROM OpcionesMenu om
        LEFT JOIN OpcionesMenu padre ON padre.IdOpcionMenu = om.IdPadre
        ORDER BY padre.Nombre, om.Nombre
        """
    )
    return render_template('admin/accesos_opciones_menu.html', opciones=opciones)


@admin_bp.route('/accesos/opciones-menu/nueva', methods=['GET', 'POST'])
@acceso_requerido
def nueva_opcion_menu():
    opciones_padre = _obtener_opciones_padre()

    if request.method == 'POST':
        conexion = conexion_actual()
        cursor = conexion.cursor()
        datos = _leer_formulario_opcion_menu(cursor, opciones_padre, id_opcion=None)
        if datos is None:
            return render_template('admin/accesos_opcion_menu_form.html', opcion=None, opciones_padre=opciones_padre)

        cursor.execute(
            """
            INSERT INTO OpcionesMenu (Nombre, UrlMenu, Descripcion, IdPadre, EstadoRegistro)
            VALUES (?, ?, ?, ?, 1)
            """,
            datos['nombre'], datos['url'], datos['descripcion'], datos['id_padre'],
        )
        conexion.commit()
        flash('Opción de menú creada correctamente.', 'success')
        return redirect(url_for('admin.lista_opciones_menu'))

    return render_template('admin/accesos_opcion_menu_form.html', opcion=None, opciones_padre=opciones_padre)


@admin_bp.route('/accesos/opciones-menu/editar/<int:id_opcion>', methods=['GET', 'POST'])
@acceso_requerido
def editar_opcion_menu(id_opcion):
    opciones_padre = _obtener_opciones_padre(excluir_id=id_opcion)
    conexion = conexion_actual()
    cursor = conexion.cursor()
    cursor.execute(
        "SELECT IdOpcionMenu, Nombre, UrlMenu, Descripcion, IdPadre, EstadoRegistro FROM OpcionesMenu WHERE IdOpcionMenu = ?",
        id_opcion,
    )
    opcion = cursor.fetchone()
    if opcion is None:
        flash('La opción de menú no existe.', 'error')
        return redirect(url_for('admin.lista_opciones_menu'))

    if request.method == 'POST':
        datos = _leer_formulario_opcion_menu(cursor, opciones_padre, id_opcion=id_opcion)
        if datos is None:
            return render_template('admin/accesos_opcion_menu_form.html', opcion=opcion, opciones_padre=opciones_padre)

        estado = 1 if request.form.get('estado') == 'on' else 0

        # La opción "Asignar Accesos" no se puede desactivar ni cambiar de URL:
        # sin ella nadie podría volver a dar permisos desde el sistema.
        cursor.execute("SELECT UrlMenu FROM OpcionesMenu WHERE IdOpcionMenu = ?", id_opcion)
        actual = cursor.fetchone()
        if actual and _normalizar_url(actual.UrlMenu) == URL_ASIGNAR_ACCESOS and (
                not estado or _normalizar_url(datos['url']) != URL_ASIGNAR_ACCESOS):
            flash('La opción "Asignar Accesos" no se puede desactivar ni cambiar de URL: '
                  'nadie podría volver a asignar permisos.', 'error')
            return redirect(url_for('admin.editar_opcion_menu', id_opcion=id_opcion))

        cursor.execute(
            """
            UPDATE OpcionesMenu
            SET Nombre = ?, UrlMenu = ?, Descripcion = ?, IdPadre = ?, EstadoRegistro = ?
            WHERE IdOpcionMenu = ?
            """,
            datos['nombre'], datos['url'], datos['descripcion'], datos['id_padre'], estado, id_opcion,
        )
        conexion.commit()
        flash('Opción de menú actualizada. Los usuarios verán el cambio en su siguiente inicio de sesión.', 'success')
        return redirect(url_for('admin.lista_opciones_menu'))

    return render_template('admin/accesos_opcion_menu_form.html', opcion=opcion, opciones_padre=opciones_padre)


def _obtener_opciones_padre(excluir_id=None):
    """
    Opciones que pueden ser 'padre' de un submenú: activas y de NIVEL SUPERIOR
    (sin padre propio). El menú tiene dos niveles, así nunca se forman ciclos
    (A hija de B y B hija de A) ni nietas que no se podrían mostrar.
    """
    opciones = consultar_todos(
        "SELECT IdOpcionMenu, Nombre FROM OpcionesMenu "
        "WHERE EstadoRegistro = 1 AND IdPadre IS NULL AND IdOpcionMenu <> ? ORDER BY Nombre",
        excluir_id or 0,
    )
    return opciones


def _leer_formulario_opcion_menu(cursor, opciones_padre, id_opcion):
    """
    Valida la opción de menú (límites = columnas de OpcionesMenu). id_opcion=None es
    alta nueva. Devuelve el dict de datos, o None si algo no es válido (ya con el flash).
    """
    nombre = ' '.join(request.form.get('nombre', '').split())
    url = request.form.get('url', '').strip()
    descripcion = ' '.join(request.form.get('descripcion', '').split()) or None
    id_padre = request.form.get('id_padre', '').strip()

    if not nombre:
        flash('El nombre de la opción es obligatorio.', 'error')
        return None
    if len(nombre) > 50:
        flash('El nombre admite máximo 50 caracteres.', 'error')
        return None
    # Solo rutas internas del panel: así nunca se puede apuntar a un sitio externo.
    if not re.fullmatch(r'/admin(/[A-Za-z0-9_-]+)+', url) or len(url) > 50:
        flash('La URL debe ser una ruta del panel, por ejemplo /admin/productos '
              '(solo letras, números, "-", "_" y "/"; máximo 50 caracteres).', 'error')
        return None
    if descripcion and len(descripcion) > 100:
        flash('La descripción admite máximo 100 caracteres.', 'error')
        return None

    cursor.execute(
        "SELECT Nombre FROM OpcionesMenu WHERE UrlMenu = ? AND IdOpcionMenu <> ?", url, id_opcion or 0,
    )
    duplicada = cursor.fetchone()
    if duplicada:
        flash(f'La URL {url} ya la usa la opción "{duplicada.Nombre}".', 'error')
        return None

    if id_padre:
        if not id_padre.isdigit() or int(id_padre) not in {p.IdOpcionMenu for p in opciones_padre}:
            flash('El menú padre elegido no es válido (debe ser una opción activa de nivel superior).', 'error')
            return None
        # Una opción que ya tiene hijas no puede volverse hija: quedarían nietas sin mostrarse.
        if id_opcion:
            cursor.execute("SELECT COUNT(*) FROM OpcionesMenu WHERE IdPadre = ?", id_opcion)
            if cursor.fetchone()[0]:
                flash('Esta opción tiene opciones hijas, así que no puede ser a su vez hija de otra.', 'error')
                return None

    return {
        'nombre': nombre,
        'url': url,
        'descripcion': descripcion,
        'id_padre': int(id_padre) if id_padre else None,
    }


# ==========================================================================
# ASIGNAR ACCESOS (qué opciones de menú ve cada perfil)
# ==========================================================================
@admin_bp.route('/accesos/asignar', methods=['GET', 'POST'])
@acceso_requerido
def asignar_accesos():
    conexion = conexion_actual()
    cursor = conexion.cursor()

    # Solo perfiles internos activos (el Cliente no entra al panel).
    cursor.execute("SELECT IdPerfil, Nombre FROM Perfiles WHERE EstadoRegistro = 1 AND IdPerfil <> ? ORDER BY Nombre",
                   ID_PERFIL_CLIENTE)
    perfiles = [{'id': fila.IdPerfil, 'nombre': fila.Nombre} for fila in cursor.fetchall()]

    id_perfil_seleccionado = request.values.get('id_perfil', type=int)
    if id_perfil_seleccionado and id_perfil_seleccionado not in {p['id'] for p in perfiles}:
        flash('El perfil elegido no existe o está anulado.', 'error')
        id_perfil_seleccionado = None

    if request.method == 'POST' and id_perfil_seleccionado:
        # Solo se aceptan opciones que existan y estén activas (las enviadas a mano se descartan).
        cursor.execute("SELECT IdOpcionMenu FROM OpcionesMenu WHERE EstadoRegistro = 1")
        ids_validos = {f.IdOpcionMenu for f in cursor.fetchall()}
        ids_marcados = list(dict.fromkeys(int(i) for i in request.form.getlist('opciones')
                                          if i.isdigit() and int(i) in ids_validos))

        # Protección contra quedarse fuera: el perfil con el que tienes la sesión
        # abierta no puede perder "Asignar Accesos" (los permisos ahora salen de aquí).
        quitandose_asignar = False
        if id_perfil_seleccionado == session.get('id_perfil'):
            cursor.execute("SELECT IdOpcionMenu, UrlMenu FROM OpcionesMenu WHERE EstadoRegistro = 1")
            ids_asignar = {f.IdOpcionMenu for f in cursor.fetchall()
                           if _normalizar_url(f.UrlMenu) == URL_ASIGNAR_ACCESOS}
            quitandose_asignar = not (ids_asignar & set(ids_marcados))

        if quitandose_asignar:
            flash('No puedes quitarle "Asignar Accesos" al perfil con el que tienes la sesión abierta: '
                  'nadie podría volver a asignar permisos.', 'error')
        else:
            # Estrategia simple: borrar las asignaciones actuales de este perfil
            # y volver a insertar solo las que quedaron marcadas en el formulario.
            cursor.execute("DELETE FROM OpcionesMenu_Perfiles WHERE IdPerfil = ?", id_perfil_seleccionado)
            for orden, id_opcion in enumerate(ids_marcados, start=1):
                cursor.execute(
                    "INSERT INTO OpcionesMenu_Perfiles (IdOpcionMenu, IdPerfil, Orden, EstadoRegistro) VALUES (?, ?, ?, 1)",
                    id_opcion, id_perfil_seleccionado, orden,
                )
            conexion.commit()
            flash('Accesos actualizados. Los usuarios de ese perfil verán el menú nuevo en su '
                  'siguiente inicio de sesión; los permisos se aplican de inmediato.', 'success')

    # Traer todas las opciones de menú activas, y marcar cuáles ya tiene asignadas el perfil elegido
    cursor.execute(
        """
        SELECT om.IdOpcionMenu, om.Nombre, om.UrlMenu, padre.Nombre AS NombrePadre
        FROM OpcionesMenu om
        LEFT JOIN OpcionesMenu padre ON padre.IdOpcionMenu = om.IdPadre
        WHERE om.EstadoRegistro = 1
        ORDER BY padre.Nombre, om.Nombre
        """
    )
    todas_las_opciones = cursor.fetchall()

    ids_asignados = set()
    if id_perfil_seleccionado:
        cursor.execute(
            "SELECT IdOpcionMenu FROM OpcionesMenu_Perfiles WHERE IdPerfil = ? AND EstadoRegistro = 1",
            id_perfil_seleccionado,
        )
        ids_asignados = {fila.IdOpcionMenu for fila in cursor.fetchall()}

    return render_template(
        'admin/accesos_asignar.html',
        perfiles=perfiles,
        id_perfil_seleccionado=id_perfil_seleccionado,
        opciones=todas_las_opciones,
        ids_asignados=ids_asignados,
    )
