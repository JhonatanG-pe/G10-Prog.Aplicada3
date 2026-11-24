"""
Módulo de PROVEEDORES.
Quién entra lo decide la opción de menú asignada a cada perfil en
"Asignar Accesos" (acceso_requerido).
"""
import re
from types import SimpleNamespace
import pyodbc
from flask import render_template, request, redirect, url_for, flash, session
from db import conexion_actual, consultar_uno
from paginacion import consultar_pagina
from routes.administrador import admin_bp, acceso_requerido


@admin_bp.route('/proveedores')
@acceso_requerido
def lista_proveedores():
    proveedores = consultar_pagina(
        """
        SELECT pr.IdProveedor, pr.RazonSocial, pr.NumDocIdentidad, pr.Contacto, pr.Correo,
               pr.Telefono, pr.Activo, u.DES_DIST, u.DES_PROV,
               (SELECT COUNT(*) FROM Compra c WHERE c.IdProveedor = pr.IdProveedor) AS Compras
        FROM Proveedor pr
        LEFT JOIN UBIGEO u ON u.UBIGEO = pr.Ubigeo
        """,
        orden='pr.RazonSocial, pr.IdProveedor',
    )
    return render_template('admin/proveedores.html', proveedores=proveedores)


@admin_bp.route('/proveedores/nuevo', methods=['GET', 'POST'])
@acceso_requerido
def nuevo_proveedor():
    ubigeos = _obtener_ubigeos()

    if request.method == 'POST':
        datos = _leer_formulario_proveedor(ubigeos)
        if datos is None:
            return render_template('admin/proveedor_form.html', proveedor=_proveedor_desde_formulario(),
                                   es_nuevo=True, ubigeos=ubigeos)

        conexion = conexion_actual()
        cursor = conexion.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO Proveedor (RazonSocial, NumDocIdentidad, Contacto, Correo, Telefono,
                                        Domicilio, Ubigeo, Activo, IdUsuario)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                datos['razon_social'], datos['num_doc'], datos['contacto'], datos['correo'], datos['telefono'],
                datos['domicilio'], datos['ubigeo'], session['id_usuario'],
            )
        except pyodbc.IntegrityError:
            # Otro usuario registró el mismo RUC justo antes (índice único UQ_Proveedor_NumDocIdentidad).
            conexion.rollback()
            flash(f'El RUC {datos["num_doc"]} ya está registrado para otro proveedor.', 'error')
            return render_template('admin/proveedor_form.html', proveedor=_proveedor_desde_formulario(),
                                   es_nuevo=True, ubigeos=ubigeos)
        conexion.commit()
        flash('Proveedor creado correctamente.', 'success')
        return redirect(url_for('admin.lista_proveedores'))

    return render_template('admin/proveedor_form.html', proveedor=None, es_nuevo=True, ubigeos=ubigeos)


@admin_bp.route('/proveedores/editar/<int:id_proveedor>', methods=['GET', 'POST'])
@acceso_requerido
def editar_proveedor(id_proveedor):
    ubigeos = _obtener_ubigeos()
    conexion = conexion_actual()
    cursor = conexion.cursor()
    cursor.execute(
        """
        SELECT IdProveedor, RazonSocial, NumDocIdentidad, Contacto, Correo, Telefono,
               Domicilio, Ubigeo, Activo
        FROM Proveedor WHERE IdProveedor = ?
        """,
        id_proveedor,
    )
    proveedor = cursor.fetchone()
    if proveedor is None:
        flash('El proveedor no existe.', 'error')
        return redirect(url_for('admin.lista_proveedores'))

    if request.method == 'POST':
        datos = _leer_formulario_proveedor(ubigeos, id_proveedor)
        if datos is None:
            # Se vuelve a mostrar lo que el usuario escribió, para que solo corrija el campo con error.
            return render_template('admin/proveedor_form.html', proveedor=_proveedor_desde_formulario(proveedor),
                                   es_nuevo=False, ubigeos=ubigeos)

        # Solo se actualiza la ficha del proveedor. Las compras guardan el IdProveedor
        # y sus propios montos: ninguna compra registrada se modifica (HU-10).
        # Anular/reactivar se hace con los botones de la lista, que validan las compras.
        try:
            cursor.execute(
                """
                UPDATE Proveedor
                SET RazonSocial = ?, NumDocIdentidad = ?, Contacto = ?, Correo = ?, Telefono = ?,
                    Domicilio = ?, Ubigeo = ?
                WHERE IdProveedor = ?
                """,
                datos['razon_social'], datos['num_doc'], datos['contacto'], datos['correo'], datos['telefono'],
                datos['domicilio'], datos['ubigeo'], id_proveedor,
            )
        except pyodbc.IntegrityError:
            conexion.rollback()
            flash(f'El RUC {datos["num_doc"]} ya está registrado para otro proveedor.', 'error')
            return render_template('admin/proveedor_form.html', proveedor=_proveedor_desde_formulario(proveedor),
                                   es_nuevo=False, ubigeos=ubigeos)
        conexion.commit()
        flash('Proveedor actualizado.', 'success')
        return redirect(url_for('admin.lista_proveedores'))

    return render_template('admin/proveedor_form.html', proveedor=proveedor, es_nuevo=False, ubigeos=ubigeos)


@admin_bp.route('/proveedores/estado/<int:id_proveedor>', methods=['POST'])
@acceso_requerido
def cambiar_estado_proveedor(id_proveedor):
    """Anula (Activo = 0) o reactiva (1) un proveedor. Nunca se borra."""
    reactivar = request.form.get('accion') == 'reactivar'
    conexion = conexion_actual()
    cursor = conexion.cursor()

    cursor.execute("SELECT RazonSocial FROM Proveedor WHERE IdProveedor = ?", id_proveedor)
    proveedor = cursor.fetchone()
    if proveedor is None:
        flash('El proveedor no existe.', 'error')
        return redirect(url_for('admin.lista_proveedores'))

    if not reactivar:
        # Regla HU-10: un proveedor con compras registradas no se puede anular,
        # para no romper el historial de compras.
        cursor.execute("SELECT COUNT(*) FROM Compra WHERE IdProveedor = ?", id_proveedor)
        compras = cursor.fetchone()[0]
        if compras:
            flash(f'No se puede anular al proveedor "{proveedor.RazonSocial}" porque tiene {compras} '
                  f'compra(s) registrada(s) en el historial.', 'error')
            return redirect(url_for('admin.lista_proveedores'))

    cursor.execute("UPDATE Proveedor SET Activo = ? WHERE IdProveedor = ?", 1 if reactivar else 0, id_proveedor)
    conexion.commit()
    flash(f'Proveedor "{proveedor.RazonSocial}" ' + ('reactivado.' if reactivar else 'anulado.'), 'success')
    return redirect(url_for('admin.lista_proveedores'))


def _obtener_ubigeos():
    """Trae los 1874 ubigeos (código de 6 dígitos + nombres) para armar las listas en cascada."""
    conexion = conexion_actual()
    cursor = conexion.cursor()
    cursor.execute(
        """
        -- TRIM: algunos nombres de la tabla traen espacios sobrantes (ej. 'Lima ')
        SELECT UBIGEO, TRIM(DES_DPTO) AS DES_DPTO, TRIM(DES_PROV) AS DES_PROV, TRIM(DES_DIST) AS DES_DIST
        FROM UBIGEO
        ORDER BY 2, 3, 4
        """
    )
    ubigeos = [[fila.UBIGEO, fila.DES_DPTO, fila.DES_PROV, fila.DES_DIST] for fila in cursor.fetchall()]
    return ubigeos


def _proveedor_desde_formulario(proveedor_guardado=None):
    """Arma un 'proveedor' con lo enviado en el formulario, para volver a mostrarlo si hubo un error."""
    return SimpleNamespace(
        RazonSocial=request.form.get('razon_social', ''),
        NumDocIdentidad=request.form.get('num_doc', ''),
        Contacto=request.form.get('contacto', ''),
        Correo=request.form.get('correo', ''),
        Telefono=request.form.get('telefono', ''),
        Domicilio=request.form.get('domicilio', ''),
        Ubigeo=request.form.get('ubigeo', ''),
        Activo=proveedor_guardado.Activo if proveedor_guardado else True,
    )


def _leer_formulario_proveedor(ubigeos, id_proveedor=None):
    """id_proveedor=None significa alta nueva; en edición se pasa el id del proveedor que se modifica."""
    razon_social = ' '.join(request.form.get('razon_social', '').split())
    num_doc = request.form.get('num_doc', '').strip() or None
    contacto = ' '.join(request.form.get('contacto', '').split()) or None
    correo = request.form.get('correo', '').strip() or None
    telefono = request.form.get('telefono', '').strip() or None
    domicilio = ' '.join(request.form.get('domicilio', '').split()) or None
    ubigeo = request.form.get('ubigeo', '').strip() or None

    # Largos máximos = tamaño de cada columna de la tabla Proveedor.
    if not razon_social:
        flash('La razón social es obligatoria.', 'error')
        return None
    if len(razon_social) > 150:
        flash('La razón social admite máximo 150 caracteres.', 'error')
        return None

    # HU-10: el proveedor se registra con contacto y ubigeo.
    if not contacto:
        flash('El contacto es obligatorio.', 'error')
        return None
    if len(contacto) > 100:
        flash('El contacto admite máximo 100 caracteres.', 'error')
        return None
    if not ubigeo:
        flash('Selecciona el departamento, la provincia y el distrito.', 'error')
        return None

    if not correo:
        flash('El correo es obligatorio.', 'error')
        return None
    if len(correo) > 100 or not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', correo):
        flash('Ingresa un correo electrónico válido (máximo 100 caracteres).', 'error')
        return None
    if domicilio and len(domicilio) > 200:
        flash('El domicilio admite máximo 200 caracteres.', 'error')
        return None

    # El HTML ya restringe el formato, pero se vuelve a validar aquí porque el
    # navegador se puede saltar (formulario editado a mano, petición directa, etc.).
    if not num_doc or not re.fullmatch(r'\d{11}', num_doc):
        flash('El RUC es obligatorio y debe tener exactamente 11 dígitos numéricos.', 'error')
        return None

    # El RUC no puede repetirse entre proveedores. Al editar se excluye al propio
    # proveedor: así puede guardar su mismo RUC, pero no uno que ya tenga otro.
    duplicado = consultar_uno(
        "SELECT RazonSocial FROM Proveedor WHERE NumDocIdentidad = ? AND IdProveedor <> ?",
        num_doc, id_proveedor or 0,
    )
    if duplicado:
        flash(f'El RUC {num_doc} ya está registrado para el proveedor "{duplicado.RazonSocial}".', 'error')
        return None

    if telefono and not re.fullmatch(r'\d{6,11}', telefono):
        flash('El teléfono debe tener entre 6 y 11 dígitos numéricos.', 'error')
        return None

    if ubigeo not in {u[0] for u in ubigeos}:
        flash('Selecciona un distrito válido.', 'error')
        return None

    return {
        'razon_social': razon_social,
        'num_doc': num_doc,
        'contacto': contacto,
        'correo': correo,
        'telefono': telefono,
        'domicilio': domicilio,
        'ubigeo': ubigeo,
    }
