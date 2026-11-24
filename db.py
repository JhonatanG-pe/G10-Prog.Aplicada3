"""
Capa de acceso a datos: conexión centralizada a SQL Server.

Las rutas nunca abren ni cierran conexiones por su cuenta:
  * conexion_actual()   -> la conexión de la petición en curso (una sola por petición).
                           Se usa cuando hay que escribir y confirmar con commit()/rollback().
  * consultar_todos()   -> ejecuta una consulta y devuelve todas las filas.
  * consultar_uno()     -> ejecuta una consulta y devuelve la primera fila (o None).
  * resultado_procedimiento() -> lee la fila que devuelve un procedimiento almacenado
                           (sql/stored_procedures.sql) y deja libre la conexión.
Las listas largas se leen por páginas con paginacion.consultar_pagina().

La conexión se cierra SIEMPRE al terminar la petición (cerrar_conexion, registrada
en app.py), aunque ocurra un error: así nunca quedan conexiones abiertas. pyodbc
además reutiliza las conexiones físicas (pool de ODBC), por lo que abrir una por
petición no satura SQL Server cuando aumentan los usuarios.
"""
import os
import pyodbc
from dotenv import load_dotenv
from flask import g

# El .env se busca SIEMPRE en la carpeta del proyecto (junto a este archivo),
# sin importar desde qué carpeta se ejecute python.
RUTA_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')


def _codificacion_env():
    """
    El Bloc de notas y PowerShell pueden guardar el .env en UTF-16 o con BOM;
    python-dotenv, por defecto, solo entiende UTF-8 sin BOM y en esos casos no
    encuentra ninguna variable. Se detecta la codificación por los primeros bytes.
    """
    try:
        with open(RUTA_ENV, 'rb') as archivo:
            inicio = archivo.read(2)
    except OSError:
        return 'utf-8'
    return 'utf-16' if inicio in (b'\xff\xfe', b'\xfe\xff') else 'utf-8-sig'


# Carga las variables del archivo .env (DB_SERVER, DB_NAME, etc.)
load_dotenv(RUTA_ENV, encoding=_codificacion_env())

# Driver ODBC instalado en cada PC. Se puede cambiar en el .env con
# DB_DRIVER=ODBC Driver 18 for SQL Server; si no se indica, se usa el 17.
DRIVER_POR_DEFECTO = 'ODBC Driver 17 for SQL Server'


def obtener_conexion():
    """
    Abre y devuelve una conexión NUEVA. Dentro de la aplicación web se usa
    conexion_actual(); esta función queda para scripts de consola (crear_usuario.py).
    """
    driver = os.environ.get('DB_DRIVER', '').strip() or DRIVER_POR_DEFECTO
    if driver not in pyodbc.drivers():
        # Mensaje claro en vez del IM002 genérico de Windows ("no se encuentra el origen de datos").
        instalados = [d for d in pyodbc.drivers() if 'SQL Server' in d]
        raise RuntimeError(
            f'No está instalado el driver "{driver}". '
            f'Drivers de SQL Server en esta PC: {instalados or "ninguno"}. '
            'Instala "ODBC Driver 17 (o 18) for SQL Server" (x64) o indica en el .env '
            'DB_DRIVER=<uno de los drivers instalados>.'
        )

    cadena_conexion = (
        f"DRIVER={{{driver}}};"
        f"SERVER={os.environ['DB_SERVER']};"
        f"DATABASE={os.environ['DB_NAME']};"
        f"UID={os.environ['DB_USER']};"
        f"PWD={os.environ['DB_PASSWORD']};"
        # Cifra todo el tráfico con el SQL Server del hosting (y valida su certificado).
        # Sin esto, ODBC Driver 17 conecta sin cifrar: las contraseñas que se envían
        # para calcular HASHBYTES viajarían en texto plano por internet (HU-05).
        "Encrypt=yes;"
    )
    return pyodbc.connect(cadena_conexion)


def conexion_actual():
    """
    Conexión de la petición en curso: se abre la primera vez que se pide y todas
    las consultas de esa petición la comparten. Lo que no se confirme con
    commit() se descarta al cerrarla.
    """
    if 'conexion_bd' not in g:
        g.conexion_bd = obtener_conexion()
    return g.conexion_bd


def cerrar_conexion(_error=None):
    """Se ejecuta al final de cada petición (app.teardown_appcontext), haya error o no."""
    conexion = g.pop('conexion_bd', None)
    if conexion is not None:
        conexion.close()


def _descartar_pendientes(cursor):
    """
    Un procedimiento almacenado, además de su fila de resultado, deja pendiente en la
    conexión su estado de retorno. Mientras no se descarte, la conexión queda "ocupada"
    y la siguiente consulta, commit() o rollback() falla. Aquí se descarta.
    """
    while cursor.nextset():
        pass


def resultado_procedimiento(cursor):
    """Fila que devuelve un procedimiento (EXEC ya ejecutado en ese cursor), dejando libre la conexión."""
    fila = cursor.fetchone()
    _descartar_pendientes(cursor)
    return fila


def consultar_todos(sql, *parametros):
    """Ejecuta una consulta (o un EXEC de procedimiento) y devuelve la lista de filas."""
    cursor = conexion_actual().cursor()
    cursor.execute(sql, *parametros)
    filas = cursor.fetchall()
    _descartar_pendientes(cursor)
    return filas


def consultar_uno(sql, *parametros):
    """Ejecuta una consulta (o un EXEC de procedimiento) y devuelve la primera fila, o None si no hay."""
    cursor = conexion_actual().cursor()
    cursor.execute(sql, *parametros)
    return resultado_procedimiento(cursor)
