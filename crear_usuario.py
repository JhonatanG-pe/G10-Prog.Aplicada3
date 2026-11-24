"""
Script interactivo para crear un usuario interno (Administrador u Operador)
desde la consola. Aplica las mismas reglas que el formulario web de Usuarios.

La contraseña se guarda con HASHBYTES('SHA2_256', clave + salt) nativo de
SQL Server, usando exactamente la misma expresión que el login (seguridad.py):
si el hash se calculara distinto, el usuario creado nunca podría ingresar.
La contraseña se pide con getpass: no se ve en pantalla mientras se escribe.

Ejecutar con: python crear_usuario.py
"""
import re
from datetime import datetime
from getpass import getpass
from db import obtener_conexion
from seguridad import SQL_HASH_CLAVE, validar_clave_nueva, generar_salt_unica
from routes.administrador import ID_PERFIL_CLIENTE


def pedir(mensaje, validar, obligatorio=True):
    """Repite la pregunta hasta que la respuesta sea válida. validar() devuelve un error o None."""
    while True:
        valor = input(mensaje).strip()
        if not valor and not obligatorio:
            return None
        error = 'Este dato es obligatorio.' if not valor else validar(valor)
        if not error:
            return valor
        print(f"  ✖ {error}")


def pedir_clave():
    while True:
        clave = getpass("Contraseña (no se mostrará): ")
        error = validar_clave_nueva(clave, getpass("Confirmar contraseña: "))
        if not error:
            return clave
        print(f"  ✖ {error}")


def main():
    conexion = obtener_conexion()
    cursor = conexion.cursor()

    def correo_valido(correo):
        if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', correo) or len(correo) > 40:
            return 'Correo inválido (máximo 40 caracteres).'
        cursor.execute("SELECT 1 FROM Usuario WHERE CorreoElectronico = ?", correo)
        return 'Ya existe un usuario con ese correo.' if cursor.fetchone() else None

    def dni_valido(dni):
        if not re.fullmatch(r'\d{8}', dni):
            return 'El DNI debe tener exactamente 8 dígitos numéricos.'
        cursor.execute("SELECT 1 FROM Usuario WHERE DNI = ?", dni)
        return 'Ya existe un usuario con ese DNI.' if cursor.fetchone() else None

    def texto_40(texto):
        return 'Máximo 40 caracteres.' if len(texto) > 40 else None

    cursor.execute(
        "SELECT IdPerfil, Nombre FROM Perfiles WHERE EstadoRegistro = 1 AND IdPerfil <> ? ORDER BY Nombre",
        ID_PERFIL_CLIENTE,
    )
    perfiles = {str(f.IdPerfil): f.Nombre for f in cursor.fetchall()}
    if not perfiles:
        print("No hay perfiles de personal interno activos en la tabla Perfiles.")
        raise SystemExit(1)

    print("=== Crear nuevo usuario interno ===\n")
    dni = pedir("DNI (8 dígitos): ", dni_valido)
    nombres = pedir("Nombres: ", texto_40)
    apellido_paterno = pedir("Apellido paterno: ", texto_40)
    apellido_materno = pedir("Apellido materno (Enter para omitir): ", texto_40, obligatorio=False)
    celular = pedir("Celular (Enter para omitir): ",
                    lambda c: None if re.fullmatch(r'\d{1,12}', c) else 'Solo números, hasta 12 dígitos.',
                    obligatorio=False)
    correo = pedir("Correo electrónico: ", correo_valido)
    clave = pedir_clave()

    print("\nPerfiles disponibles:")
    for id_perfil, nombre in perfiles.items():
        print(f"  {id_perfil} - {nombre}")
    id_perfil = pedir("IdPerfil a asignar: ", lambda p: None if p in perfiles else 'Elige un IdPerfil de la lista.')

    salt = generar_salt_unica(cursor)
    cursor.execute(
        f"""
        INSERT INTO Usuario (DNI, Nombres, ApellidoPaterno, ApellidoMaterno, Celular,
                              CorreoElectronico, Clave, Salt, FechaCreacion, EstadoRegistro)
        OUTPUT INSERTED.IdUsuario
        VALUES (?, ?, ?, ?, ?, ?, {SQL_HASH_CLAVE}, ?, ?, 1)
        """,
        dni, nombres, apellido_paterno, apellido_materno, celular,
        correo, clave, salt, salt, datetime.now(),
    )
    id_usuario = cursor.fetchone()[0]
    cursor.execute(
        "INSERT INTO Usuario_Perfiles (IdUsuario, IdPerfil, EstadoRegistro) VALUES (?, ?, 1)",
        id_usuario, int(id_perfil),
    )
    conexion.commit()
    conexion.close()

    print(f"\n✅ Usuario creado correctamente. IdUsuario = {id_usuario}, perfil {perfiles[id_perfil]}.")
    print(f"   Ya puede iniciar sesión con el correo '{correo}'.")


if __name__ == '__main__':
    main()
