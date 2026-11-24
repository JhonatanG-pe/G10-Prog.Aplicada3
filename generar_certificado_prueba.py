"""
Genera un certificado digital AUTOFIRMADO para el entorno de PRUEBAS (beta) de SUNAT.

    python generar_certificado_prueba.py

Crea certificados/certificado_prueba.pem (clave privada + certificado). SUNAT beta
acepta cualquier certificado; en producción se usa el certificado digital real de la
empresa (archivo .pfx), indicado en el .env con SUNAT_CERTIFICADO y SUNAT_CERTIFICADO_CLAVE.

El archivo contiene una CLAVE PRIVADA: no se comparte ni se sube a internet.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

CARPETA = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'certificados')
RUTA = os.path.join(CARPETA, 'certificado_prueba.pem')

if os.path.exists(RUTA) and '--reemplazar' not in sys.argv:
    print(f'Ya existe {RUTA}. Para generar otro: python generar_certificado_prueba.py --reemplazar')
    sys.exit(0)

llave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
nombre = x509.Name([
    x509.NameAttribute(NameOID.COUNTRY_NAME, 'PE'),
    x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'CERTIFICADO DE PRUEBA'),
    x509.NameAttribute(NameOID.COMMON_NAME, '20000000001 PRUEBAS SUNAT BETA'),
])
ahora = datetime.now(timezone.utc)
certificado = (
    x509.CertificateBuilder()
    .subject_name(nombre).issuer_name(nombre)
    .public_key(llave.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(ahora - timedelta(days=1))
    .not_valid_after(ahora + timedelta(days=3650))
    .sign(llave, hashes.SHA256())
)

os.makedirs(CARPETA, exist_ok=True)
with open(RUTA, 'wb') as archivo:
    archivo.write(llave.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    archivo.write(certificado.public_bytes(serialization.Encoding.PEM))
print(f'Certificado de prueba creado: {RUTA}')
