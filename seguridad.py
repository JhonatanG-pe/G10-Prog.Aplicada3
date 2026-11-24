"""
Reglas de contraseñas (HU-05), compartidas por el panel interno, la tienda
del cliente y el script crear_usuario.py, para que todos guarden y comparen
el hash exactamente igual.

Esquema: Clave = HASHBYTES('SHA2_256', clave + salt), calculado SIEMPRE por
SQL Server (columna Clave VARBINARY(32) + columna Salt VARCHAR(32)).
"""
import re
import secrets

LARGO_MINIMO_CLAVE = 8
LARGO_MAXIMO_CLAVE = 64

# Expresión SQL única para calcular el hash. El CAST a VARCHAR es obligatorio:
# pyodbc envía los textos como NVARCHAR, y HASHBYTES de un NVARCHAR da otro
# resultado — sin el CAST el login nunca reconocería la contraseña.
SQL_HASH_CLAVE = "HASHBYTES('SHA2_256', CAST(? AS VARCHAR(255)) + CAST(? AS VARCHAR(32)))"

# La comparación del login y el cambio de contraseña del cliente se hacen dentro de
# los procedimientos usp_Usuario_Login, usp_Cliente_Login y usp_Cliente_CambiarClave
# (sql/stored_procedures.sql), con la misma fórmula: sus parámetros @Clave son
# VARCHAR por la misma razón que el CAST de arriba.

# Mismas reglas que el atributo pattern de los <input type="password">.
_TIENE_LETRA = re.compile(r'[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]')
_TIENE_NUMERO = re.compile(r'[0-9]')


def _representable_en_varchar(clave):
    """
    La BD usa la página de códigos 1252 (collation SQL_Latin1_General_CP1).
    Un carácter fuera de ella (emojis, etc.) se convierte en '?' al hacer
    CAST a VARCHAR: 'clave😀' y 'clave🔥' darían el MISMO hash. Se rechazan.
    """
    try:
        clave.encode('cp1252')
    except UnicodeEncodeError:
        return False
    return all(c.isprintable() for c in clave)


def validar_clave_nueva(clave, clave_confirmar=None):
    """
    Política para registrar o cambiar una contraseña.
    Devuelve el mensaje de error, o None si la contraseña es válida.
    """
    if not clave:
        return 'La contraseña es obligatoria.'
    if clave != clave.strip():
        return 'La contraseña no puede empezar ni terminar con espacios.'
    if not LARGO_MINIMO_CLAVE <= len(clave) <= LARGO_MAXIMO_CLAVE:
        return f'La contraseña debe tener entre {LARGO_MINIMO_CLAVE} y {LARGO_MAXIMO_CLAVE} caracteres.'
    if not _TIENE_LETRA.search(clave) or not _TIENE_NUMERO.search(clave):
        return 'La contraseña debe tener al menos una letra y un número.'
    if not _representable_en_varchar(clave):
        return 'La contraseña contiene caracteres no permitidos (por ejemplo, emojis).'
    if clave_confirmar is not None and clave != clave_confirmar:
        return 'Las contraseñas no coinciden.'
    return None


def clave_ingresada_es_comparable(clave):
    """
    Filtro del login: no exige la política completa (las contraseñas antiguas
    pueden no cumplirla), pero descarta lo que nunca puede coincidir de forma
    segura: vacía, demasiado larga (se recortaría a 255) o con caracteres que
    SQL Server convertiría en '?'.
    """
    return bool(clave) and len(clave) <= LARGO_MAXIMO_CLAVE and _representable_en_varchar(clave)


def generar_salt_unica(cursor):
    """
    Salt aleatoria de 32 caracteres hexadecimales (16 bytes de secrets, criptográficamente
    segura). Se verifica que no exista ya en Usuario ni en Cliente: la probabilidad de
    repetirse es prácticamente nula, pero la HU-05 exige que sea única.
    """
    for _ in range(5):
        salt = secrets.token_hex(16)
        cursor.execute(
            "SELECT 1 WHERE EXISTS (SELECT 1 FROM Usuario WHERE Salt = ?) "
            "OR EXISTS (SELECT 1 FROM Cliente WHERE Salt = ?)",
            salt, salt,
        )
        if cursor.fetchone() is None:
            return salt
    raise RuntimeError('No se pudo generar una salt única.')
