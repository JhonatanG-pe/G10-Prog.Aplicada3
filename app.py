import os
import sys
from flask import Flask, redirect, url_for, flash, request

import db  # carga el .env (con detección de codificación) antes que cualquier otro módulo
import paginacion
from routes.administrador import admin_bp
from routes import catalogo      # registra rutas de categorías/marcas/productos en admin_bp
from routes import accesos       # registra rutas de perfiles/opciones de menú/asignación en admin_bp
from routes import proveedores   # registra rutas de proveedores en admin_bp
from routes import compras       # registra rutas de compras (reposición de stock) en admin_bp
from routes import reportes      # registra rutas de reportes en admin_bp
from routes import pedidos       # registra rutas de pedidos de la tienda (HU-20) en admin_bp
from routes.cliente import cliente_bp
from routes.api import api_bp

VARIABLES_OBLIGATORIAS = ('DB_SERVER', 'DB_NAME', 'DB_USER', 'DB_PASSWORD', 'SECRET_KEY')


def _verificar_configuracion():
    """
    Si falta el .env o alguno de sus datos, explica exactamente qué pasa y cómo
    arreglarlo, en vez de un KeyError difícil de entender. Luego detiene el sistema.
    """
    faltantes = [v for v in VARIABLES_OBLIGATORIAS if not os.environ.get(v, '').strip()]
    if not faltantes:
        return

    carpeta = os.path.dirname(db.RUTA_ENV)
    print('\n' + '=' * 78)
    print(' NO SE PUEDE INICIAR EL SISTEMA: problema con el archivo .env')
    print('=' * 78)
    if not os.path.isfile(db.RUTA_ENV):
        print(f' No existe el archivo:\n   {db.RUTA_ENV}')
        parecidos = [f for f in os.listdir(carpeta) if f.lower().lstrip('.').startswith('env') and f != '.env.example']
        if parecidos:
            print(f'\n Pero en la carpeta hay: {", ".join(parecidos)}')
            print(' Windows probablemente le agregó una extensión. Renómbralo a ".env", por ejemplo:')
            print(f'   Rename-Item "{parecidos[0]}" .env')
        else:
            print('\n Copia tu archivo .env en esa carpeta (la misma donde está app.py).')
    else:
        print(f' El archivo existe ({db.RUTA_ENV}),')
        print(f' pero le faltan estos datos (o están vacíos): {", ".join(faltantes)}')
        print('\n Ábrelo con el Bloc de notas y revisa que tenga una línea por dato, por ejemplo:')
        print('   SECRET_KEY=un-texto-largo-y-aleatorio')
        print(' (sin espacios antes del nombre y sin comillas extra).')
    print('=' * 78 + '\n')
    sys.exit(1)


_verificar_configuracion()

app = Flask(__name__)
# Sin valor por defecto: con una clave conocida cualquiera podría falsificar
# la cookie de sesión y entrar como Administrador. Si falta en el .env, la app no arranca.
app.secret_key = os.environ['SECRET_KEY']

# Tope de cada envío de formulario: la foto (máx. 2 MB, HU-09) más el resto de campos.
# Sin este tope, un archivo enorme se recibiría completo antes de poder rechazarlo.
app.config['MAX_CONTENT_LENGTH'] = (catalogo.TAMANO_MAXIMO_IMAGEN_MB + 1) * 1024 * 1024

# Una conexión a la BD por petición, cerrada siempre al terminar (aunque haya un error).
app.teardown_appcontext(db.cerrar_conexion)
# Las plantillas arman los enlaces de paginación conservando los filtros de la URL.
app.add_template_global(paginacion.url_pagina)


@app.errorhandler(413)
def envio_demasiado_grande(_error):
    flash(f'El archivo es demasiado grande. El tamaño máximo de la foto es {catalogo.TAMANO_MAXIMO_IMAGEN_MB} MB.', 'error')
    return redirect(request.url)


# Cada perfil tiene su propio módulo, con su propio login y sus propias vistas
app.register_blueprint(admin_bp)
app.register_blueprint(cliente_bp)
app.register_blueprint(api_bp)


@app.route('/')
def inicio():
    # La raíz del sitio es la tienda pública (para el cliente). El personal
    # interno sigue entrando por su propia puerta: /admin/login.
    return redirect(url_for('cliente.tienda'))


if __name__ == '__main__':
    # El modo debug muestra código y permite ejecutar comandos desde el navegador
    # si ocurre un error: solo se activa en desarrollo, con FLASK_DEBUG=1 en el .env.
    app.run(debug=os.environ.get('FLASK_DEBUG') == '1')
