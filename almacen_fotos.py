"""
Fotos de productos guardadas en el HOSTING (SmarterASP), no en la PC de quien las sube.

Por pedido del profesor la foto NUNCA se guarda en binario en la base de datos.
El archivo se sube por FTP a una carpeta del sitio web del hosting y en la tabla
Producto solo se guarda su dirección pública (RutaImagen) y su nombre (NombreImagen).
Así todas las PC que usan el sistema ven las mismas fotos.

Configuración en el .env (ver .env.example):
    FTP_SERVIDOR       servidor FTP del hosting (panel SmarterASP > FTP)
    FTP_USUARIO        usuario FTP
    FTP_CLAVE          contraseña FTP
    FTP_CARPETA        carpeta de las fotos vista desde el FTP, p. ej. /site16/Fotos
    FOTOS_URL_PUBLICA  la misma carpeta vista desde internet,
                       p. ej. http://ricardofrisancho-001-site16.ltempurl.com/Fotos
"""
import ftplib
import os
import posixpath

VARIABLES = ('FTP_SERVIDOR', 'FTP_USUARIO', 'FTP_CLAVE', 'FTP_CARPETA', 'FOTOS_URL_PUBLICA')
TIEMPO_ESPERA = 30  # segundos


class ErrorAlmacenFotos(Exception):
    """No se pudo subir o borrar la foto en el hosting (mensaje apto para mostrar en pantalla)."""


def faltantes():
    """Variables del .env que faltan para poder subir fotos al hosting."""
    return [v for v in VARIABLES if not os.environ.get(v, '').strip()]


def _carpeta():
    return '/' + os.environ['FTP_CARPETA'].strip().replace('\\', '/').strip('/')


def url_publica(nombre_archivo):
    return os.environ['FOTOS_URL_PUBLICA'].strip().rstrip('/') + '/' + nombre_archivo


def es_del_hosting(ruta_imagen):
    """True si la ruta guardada en Producto.RutaImagen apunta a la carpeta de fotos del hosting."""
    base = os.environ.get('FOTOS_URL_PUBLICA', '').strip().rstrip('/')
    return bool(base and ruta_imagen and ruta_imagen.startswith(base + '/'))


class _FTPCifrado(ftplib.FTP_TLS):
    """
    FTP cifrado (FTPS) compatible con el servidor FTP de Windows (IIS) de SmarterASP:
    IIS no contesta el aviso de cierre TLS del canal de datos y el storbinary estándar
    de Python se queda esperándolo hasta agotar el tiempo (aunque el archivo ya se subió).
    Aquí, al terminar de enviar, se cierra el canal y se lee la confirmación del servidor.
    """
    def storbinary(self, cmd, fp, blocksize=8192, callback=None, rest=None):
        self.voidcmd('TYPE I')
        with self.transfercmd(cmd, rest) as conexion:
            while bloque := fp.read(blocksize):
                conexion.sendall(bloque)
        return self.voidresp()


def _conectar():
    """Abre la conexión FTP. Intenta primero cifrada (FTPS); si el servidor no la admite, normal."""
    servidor = os.environ['FTP_SERVIDOR'].strip()
    usuario, clave = os.environ['FTP_USUARIO'].strip(), os.environ['FTP_CLAVE']
    try:
        ftp = _FTPCifrado(servidor, timeout=TIEMPO_ESPERA)
        ftp.login(usuario, clave)
        ftp.prot_p()  # cifra también los archivos que se transfieren
        return ftp
    except ftplib.error_perm as error:
        if str(error).startswith('530'):  # usuario o contraseña incorrectos: no tiene sentido reintentar
            raise
    except (ftplib.Error, OSError):
        pass
    ftp = ftplib.FTP(servidor, timeout=TIEMPO_ESPERA)
    ftp.login(usuario, clave)
    return ftp


def subir(nombre_archivo, contenido):
    """Sube la foto (objeto tipo archivo) a la carpeta del hosting y devuelve su URL pública."""
    if faltantes():
        raise ErrorAlmacenFotos('No está configurado el almacenamiento de fotos en el hosting. '
                                f'Faltan en el archivo .env: {", ".join(faltantes())}.')
    try:
        with _conectar() as ftp:
            ftp.storbinary(f'STOR {posixpath.join(_carpeta(), nombre_archivo)}', contenido)
    except ftplib.error_perm as error:
        if str(error).startswith('530'):
            raise ErrorAlmacenFotos('El hosting rechazó el usuario o la contraseña FTP del archivo .env.') from error
        raise ErrorAlmacenFotos(f'El hosting no permitió guardar la foto en {_carpeta()} ({error}). '
                                'Revisa FTP_CARPETA en el .env.') from error
    except (ftplib.Error, OSError) as error:
        raise ErrorAlmacenFotos('No se pudo conectar con el hosting para guardar la foto. '
                                'Revisa tu conexión a internet y FTP_SERVIDOR en el .env.') from error
    return url_publica(nombre_archivo)


def borrar(nombre_archivo):
    """Borra una foto del hosting. Si no se puede, no interrumpe el flujo (devuelve False)."""
    if not nombre_archivo or faltantes():
        return False
    try:
        with _conectar() as ftp:
            ftp.delete(posixpath.join(_carpeta(), nombre_archivo))
        return True
    except (ftplib.Error, OSError):
        return False


def probar():
    """Diagnóstico: sube un archivo pequeño, lo lee por internet y lo borra. Devuelve (ok, mensaje)."""
    import io
    import urllib.request
    import uuid

    nombre = f'prueba_{uuid.uuid4().hex[:8]}.txt'
    try:
        url = subir(nombre, io.BytesIO(b'ok'))
    except ErrorAlmacenFotos as error:
        return False, str(error)
    try:
        with urllib.request.urlopen(url, timeout=TIEMPO_ESPERA) as respuesta:
            visible = respuesta.read() == b'ok'
    except OSError:
        visible = False
    borrar(nombre)
    if not visible:
        return False, (f'El archivo se subió por FTP, pero no se ve en {url}. '
                       'FTP_CARPETA y FOTOS_URL_PUBLICA deben ser la MISMA carpeta.')
    return True, f'Correcto: el archivo se subió al hosting y se ve en internet ({url}).'


if __name__ == '__main__':
    import db  # noqa: F401  (carga el .env)
    exito, mensaje = probar()
    print(('OK  ' if exito else 'ERROR  ') + mensaje)
