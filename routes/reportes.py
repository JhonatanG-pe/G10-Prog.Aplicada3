"""
Módulo de REPORTES, EXCLUSIVO del perfil Administrador:
  HU-12: bajo stock, valorización de inventario e historial de compras.
  HU-17: ventas y productos más vendidos.
Cada ruta exige la opción Reportes asignada (acceso_requerido) Y además el perfil
Administrador (solo_perfil_administrador): un Operador no entra ni escribiendo la URL.

Los datos de cada reporte los arma un procedimiento almacenado (sql/stored_procedures.sql):
usp_Reporte_BajoStock, usp_Reporte_Valorizacion, usp_Reporte_HistorialCompras,
usp_Reporte_Ventas y usp_Reporte_MasVendidos. Aquí se validan los filtros.
"""
from datetime import datetime
from decimal import Decimal
from functools import wraps
from flask import render_template, request, flash, session, redirect, url_for
from db import conexion_actual, consultar_todos
from routes.administrador import admin_bp, acceso_requerido, ID_PERFIL_ADMINISTRADOR


def solo_perfil_administrador(vista):
    """
    HU-12 y HU-17: los reportes son EXCLUSIVOS del perfil Administrador. Además de
    tener asignada la opción Reportes (acceso_requerido), el perfil de la sesión debe
    ser el de Administrador. Otro perfil con Reportes asignado no puede verlos.
    """
    @wraps(vista)
    def envoltura(*args, **kwargs):
        if session.get('id_perfil') != ID_PERFIL_ADMINISTRADOR:
            flash('Los reportes son exclusivos del perfil Administrador.', 'error')
            return redirect(url_for('admin.panel'))
        return vista(*args, **kwargs)
    return envoltura


@admin_bp.route('/reportes')
@acceso_requerido
@solo_perfil_administrador
def reportes_index():
    return render_template('admin/reportes_index.html')


@admin_bp.route('/reportes/bajo-stock')
@acceso_requerido
@solo_perfil_administrador
def reporte_bajo_stock():
    # HU-12: únicamente los productos activos con Stock en o por debajo de su mínimo.
    productos = consultar_todos("EXEC dbo.usp_Reporte_BajoStock")
    return render_template('admin/reporte_bajo_stock.html', productos=productos)


@admin_bp.route('/reportes/valorizacion')
@acceso_requerido
@solo_perfil_administrador
def reporte_valorizacion():
    # HU-12: valor por producto activo = Stock x Precio, sobre el inventario actual.
    productos = consultar_todos("EXEC dbo.usp_Reporte_Valorizacion")
    # Precio es DECIMAL: la suma se hace en Decimal (exacta), sin pasar por float.
    valor_general = sum((p.ValorTotal for p in productos), Decimal('0'))
    return render_template('admin/reporte_valorizacion.html', productos=productos, valor_general=valor_general)


@admin_bp.route('/reportes/compras')
@acceso_requerido
@solo_perfil_administrador
def reporte_historial_compras():
    """HU-12: historial de compras, filtrable por proveedor y/o rango de fechas (ambos opcionales)."""
    conexion = conexion_actual()
    cursor = conexion.cursor()
    # Todos los proveedores (también los anulados): sus compras siguen en el historial.
    cursor.execute("SELECT IdProveedor, RazonSocial, Activo FROM Proveedor ORDER BY RazonSocial")
    proveedores = cursor.fetchall()

    filtros = {
        'id_proveedor': request.args.get('id_proveedor', '').strip(),
        'desde': request.args.get('desde', '').strip(),
        'hasta': request.args.get('hasta', '').strip(),
    }
    error = _validar_filtros_compras(filtros, proveedores)
    if error:
        flash(error, 'error')
        return render_template('admin/reporte_compras.html', compras=[], total_general=Decimal('0'),
                               proveedores=proveedores, filtros=filtros)

    # Filtros opcionales: None = sin filtro. "Hasta" incluye todo ese día (lo resuelve el procedimiento).
    compras = consultar_todos(
        "EXEC dbo.usp_Reporte_HistorialCompras @IdProveedor = ?, @Desde = ?, @Hasta = ?",
        int(filtros['id_proveedor']) if filtros['id_proveedor'] else None,
        _fecha_o_none(filtros['desde']), _fecha_o_none(filtros['hasta']),
    )
    total_general = sum((c.Total for c in compras), Decimal('0'))
    return render_template('admin/reporte_compras.html', compras=compras, total_general=total_general,
                           proveedores=proveedores, filtros=filtros)


def _validar_filtros_compras(filtros, proveedores):
    """Valida los filtros del historial (vienen en la URL: se pueden escribir a mano)."""
    if filtros['id_proveedor'] and (not filtros['id_proveedor'].isdigit()
                                    or int(filtros['id_proveedor']) not in {p.IdProveedor for p in proveedores}):
        return 'El proveedor seleccionado no existe.'
    return _validar_rango_fechas(filtros)


def _validar_rango_fechas(filtros):
    """Fechas 'desde'/'hasta' opcionales en formato AAAA-MM-DD, y desde <= hasta."""
    fechas = {}
    for campo, nombre in (('desde', 'Desde'), ('hasta', 'Hasta')):
        if filtros[campo]:
            try:
                fechas[campo] = datetime.strptime(filtros[campo], '%Y-%m-%d')
            except ValueError:
                return f'La fecha "{nombre}" no es válida.'
    if len(fechas) == 2 and fechas['desde'] > fechas['hasta']:
        return 'La fecha "Desde" no puede ser posterior a la fecha "Hasta".'
    return None


def _fecha_o_none(texto):
    """Fecha AAAA-MM-DD ya validada -> date para el procedimiento; vacía -> None (sin filtro)."""
    return datetime.strptime(texto, '%Y-%m-%d').date() if texto else None


# ==========================================================================
# HU-17: VENTAS (exclusivo del perfil Administrador)
# ==========================================================================

@admin_bp.route('/reportes/ventas')
@acceso_requerido
@solo_perfil_administrador
def reporte_ventas():
    """HU-17: ventas registradas por el checkout (Venta), filtrables por fecha y/o cliente."""
    conexion = conexion_actual()
    cursor = conexion.cursor()
    cursor.execute("SELECT IdCliente, Nombres, Apellidos, Correo FROM Cliente ORDER BY Nombres, Apellidos")
    clientes = cursor.fetchall()

    filtros = {
        'id_cliente': request.args.get('id_cliente', '').strip(),
        'desde': request.args.get('desde', '').strip(),
        'hasta': request.args.get('hasta', '').strip(),
    }
    error = None
    if filtros['id_cliente'] and (not filtros['id_cliente'].isdigit()
                                  or int(filtros['id_cliente']) not in {c.IdCliente for c in clientes}):
        error = 'El cliente seleccionado no existe.'
    error = error or _validar_rango_fechas(filtros)
    if error:
        flash(error, 'error')
        return render_template('admin/reporte_ventas.html', ventas=[], total_general=Decimal('0'),
                               total_unidades=0, clientes=clientes, filtros=filtros)

    # HU-17, criterio 3: el procedimiento no cuenta los pedidos anulados (HU-20).
    ventas = consultar_todos(
        "EXEC dbo.usp_Reporte_Ventas @IdCliente = ?, @Desde = ?, @Hasta = ?",
        int(filtros['id_cliente']) if filtros['id_cliente'] else None,
        _fecha_o_none(filtros['desde']), _fecha_o_none(filtros['hasta']),
    )
    return render_template('admin/reporte_ventas.html', ventas=ventas, clientes=clientes, filtros=filtros,
                           total_general=sum((v.MontoTotal for v in ventas), Decimal('0')),
                           total_unidades=sum(v.TotalProducto for v in ventas))


@admin_bp.route('/reportes/mas-vendidos')
@acceso_requerido
@solo_perfil_administrador
def reporte_mas_vendidos():
    """HU-17: ranking de productos más vendidos (por unidades), desde Detalle_Venta."""
    # HU-17, criterio 3: el procedimiento no cuenta los pedidos anulados (HU-20).
    ranking = consultar_todos("EXEC dbo.usp_Reporte_MasVendidos")
    return render_template('admin/reporte_mas_vendidos.html', ranking=ranking)
