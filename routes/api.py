import os
from functools import wraps
from flask import Blueprint, jsonify, request
from db import consultar_todos, consultar_uno

api_bp = Blueprint('api', __name__, url_prefix='/api/v1')


@api_bp.route("/", methods=["GET"])
def api_index():
    return jsonify({
        "ok": True,
        "servicio": "API Carrito de Compras - Grupo 10",
        "version": "1.0",
        "autenticacion": "Header X-API-Key requerido",
        "endpoints": [
            "GET /api/v1/health",
            "GET /api/v1/productos",
            "GET /api/v1/productos/<id>",
            "GET /api/v1/ventas/<id>"
        ]
    }), 200

def api_key_requerida(f):
    """Protege los endpoints mediante el header X-API-Key configurado en .env."""
    @wraps(f)
    def envoltura(*args, **kwargs):
        clave_esperada = os.environ.get('EXTERNAL_API_KEY', '').strip()
        if clave_esperada:
            clave_recibida = request.headers.get('X-API-Key', '').strip()
            if clave_recibida != clave_esperada:
                return jsonify({
                    'ok': False,
                    'error': 'No autorizado. Header X-API-Key inválido o ausente.'
                }), 401
        return f(*args, **kwargs)
    return envoltura


@api_bp.route('/health', methods=['GET'])
def health_check():
    """Verifica que la API esté activa."""
    return jsonify({
        'ok': True,
        'servicio': 'API Carrito de Compras - Grupo 10',
        'version': '1.0'
    }), 200


@api_bp.route('/productos', methods=['GET'])
@api_key_requerida
def listar_productos():
    """Devuelve el catálogo activo con precios y stock disponible."""
    try:
        filas = consultar_todos("""
            SELECT p.IdProducto, p.Nombre, p.Descripcion, p.Precio, p.Stock,
                   c.Descripcion AS Categoria, m.Descripcion AS Marca
            FROM dbo.Producto p
            LEFT JOIN dbo.Categoria c ON c.IdCategoria = p.IdCategoria
            LEFT JOIN dbo.Marca m ON m.IdMarca = p.IdMarca
            WHERE p.Activo = 1
            ORDER BY p.IdProducto
        """)
        productos = [
            {
                'id': r.IdProducto,
                'nombre': r.Nombre,
                'descripcion': r.Descripcion or '',
                'precio': float(r.Precio),
                'stock': int(r.Stock),
                'categoria': r.Categoria or 'General',
                'marca': r.Marca or 'Genérico'
            }
            for r in filas
        ]
        return jsonify({
            'ok': True,
            'total': len(productos),
            'productos': productos
        }), 200
    except Exception as e:
        return jsonify({
            'ok': False,
            'error': 'Error interno al consultar los productos.',
            'detalle': str(e)
        }), 500


@api_bp.route('/productos/<int:id_producto>', methods=['GET'])
@api_key_requerida
def obtener_producto(id_producto):
    """Devuelve el detalle y stock puntual de un producto por ID."""
    try:
        fila = consultar_uno("""
            SELECT p.IdProducto, p.Nombre, p.Descripcion, p.Precio, p.Stock, p.Activo,
                   c.Descripcion AS Categoria, m.Descripcion AS Marca
            FROM dbo.Producto p
            LEFT JOIN dbo.Categoria c ON c.IdCategoria = p.IdCategoria
            LEFT JOIN dbo.Marca m ON m.IdMarca = p.IdMarca
            WHERE p.IdProducto = ?
        """, (id_producto,))

        if not fila or not fila.Activo:
            return jsonify({
                'ok': False,
                'error': f'Producto con ID {id_producto} no encontrado o inactivo.'
            }), 404

        return jsonify({
            'ok': True,
            'producto': {
                'id': fila.IdProducto,
                'nombre': fila.Nombre,
                'descripcion': fila.Descripcion or '',
                'precio': float(fila.Precio),
                'stock': int(fila.Stock),
                'categoria': fila.Categoria or 'General',
                'marca': fila.Marca or 'Genérico'
            }
        }), 200
    except Exception as e:
        return jsonify({
            'ok': False,
            'error': 'Error interno al consultar el producto.',
            'detalle': str(e)
        }), 500


@api_bp.route('/ventas/<int:id_venta>', methods=['GET'])
@api_key_requerida
def consultar_venta(id_venta):
    """Consulta el estado y detalle de una venta registrada."""
    try:
        venta = consultar_uno("""
            SELECT IdVenta, TotalProducto, MontoTotal, Contacto, FechaVenta, Estado
            FROM dbo.Venta
            WHERE IdVenta = ?
        """, (id_venta,))

        if not venta:
            return jsonify({
                'ok': False,
                'error': f'Venta N° {id_venta} no encontrada.'
            }), 404

        detalles = consultar_todos("""
            SELECT dv.IdProducto, p.Nombre, dv.Cantidad, dv.Precio
            FROM dbo.Detalle_Venta dv
            INNER JOIN dbo.Producto p ON p.IdProducto = dv.IdProducto
            WHERE dv.IdVenta = ?
        """, (id_venta,))

        return jsonify({
            'ok': True,
            'venta': {
                'id_venta': venta.IdVenta,
                'cliente': venta.Contacto,
                'fecha': venta.FechaVenta.strftime('%Y-%m-%d %H:%M:%S') if venta.FechaVenta else None,
                'estado': venta.Estado,
                'total_productos': int(venta.TotalProducto),
                'monto_total': float(venta.MontoTotal),
                'items': [
                    {
                        'id_producto': d.IdProducto,
                        'nombre': d.Nombre,
                        'cantidad': int(d.Cantidad),
                        'precio_unitario': float(d.Precio)
                    }
                    for d in detalles
                ]
            }
        }), 200
    except Exception as e:
        return jsonify({
            'ok': False,
            'error': 'Error interno al consultar la venta.',
            'detalle': str(e)
        }), 500