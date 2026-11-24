"""
Facturación electrónica SUNAT: Boleta de Venta (03) y Factura (01) en formato UBL 2.1.

Flujo (Sistema de Emisión Electrónica "desde los sistemas del contribuyente"):
  1. usp_Venta_Registrar crea el Comprobante junto con la venta (serie, correlativo,
     valor de venta e IGV de cada línea), en la misma transacción.
  2. emitir() lee el comprobante (usp_Comprobante_Obtener), arma el XML UBL 2.1,
     lo firma con el certificado digital (XMLDSig, RSA-SHA256), lo comprime en un ZIP
     y lo envía al servicio web billService de SUNAT (operación sendBill).
  3. SUNAT responde con el CDR (Constancia de Recepción): aceptado, aceptado con
     observaciones o rechazado. emitir() guarda el XML firmado, el CDR y el estado
     con usp_Comprobante_RegistrarRespuesta.
  Si SUNAT no responde (sin internet, servicio caído) el comprobante queda en ERROR
  y se puede reenviar desde el panel (Pedidos > Ver > Enviar a SUNAT).

Configuración (archivo .env; ver .env.example). Por defecto apunta al entorno de
PRUEBAS (beta) de SUNAT, que acepta el RUC 20000000001, el usuario MODDATOS y un
certificado autofirmado (se genera con: python generar_certificado_prueba.py).
"""
import base64
import io
import os
import re
import ssl
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from decimal import Decimal
from xml.sax.saxutils import escape

from lxml import etree

from db import resultado_procedimiento

CARPETA_PROYECTO = os.path.dirname(os.path.abspath(__file__))
URL_BETA = 'https://e-beta.sunat.gob.pe/ol-ti-itcpfegem-beta/billService'

TIPOS_COMPROBANTE = {'01': 'FACTURA ELECTRÓNICA', '03': 'BOLETA DE VENTA ELECTRÓNICA'}
TIPOS_DOCUMENTO = {'6': 'RUC', '1': 'DNI', '0': 'DOC. TRIB. NO DOM. SIN RUC'}
# Una boleta puede emitirse sin identificar al cliente solo hasta este monto (S/).
MONTO_MAXIMO_BOLETA_SIN_DOCUMENTO = Decimal('700.00')
ESTADOS_DEFINITIVOS = ('ACEPTADO', 'OBSERVADO', 'RECHAZADO')

NS = {
    '': 'urn:oasis:names:specification:ubl:schema:xsd:Invoice-2',
    'cac': 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
    'cbc': 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2',
    'ds': 'http://www.w3.org/2000/09/xmldsig#',
    'ext': 'urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2',
}
NS_CDR = {
    'cac': NS['cac'],
    'cbc': NS['cbc'],
}


# ==========================================================================
# Configuración
# ==========================================================================
def _config():
    """Lee la configuración del .env cada vez (así un cambio no exige reiniciar)."""
    certificado = os.environ.get('SUNAT_CERTIFICADO', '').strip() or os.path.join('certificados', 'certificado_prueba.pem')
    if not os.path.isabs(certificado):
        certificado = os.path.join(CARPETA_PROYECTO, certificado)
    return {
        'url': os.environ.get('SUNAT_URL', '').strip() or URL_BETA,
        'usuario': os.environ.get('SUNAT_USUARIO_SOL', '').strip() or 'MODDATOS',
        'clave': os.environ.get('SUNAT_CLAVE_SOL', '').strip() or 'moddatos',
        'certificado': certificado,
        'clave_certificado': os.environ.get('SUNAT_CERTIFICADO_CLAVE', ''),
    }


def envio_automatico():
    """SUNAT_ENVIO_AUTOMATICO=0 desactiva el envío al confirmar la compra (queda PENDIENTE)."""
    return os.environ.get('SUNAT_ENVIO_AUTOMATICO', '1').strip() != '0'


def es_entorno_beta():
    return 'beta' in _config()['url']


# ==========================================================================
# Validaciones de documentos del cliente
# ==========================================================================
def ruc_valido(ruc):
    """RUC: 11 dígitos, empieza en 10, 15, 16, 17 o 20, y dígito verificador (módulo 11)."""
    if not re.fullmatch(r'\d{11}', ruc or '') or ruc[:2] not in ('10', '15', '16', '17', '20'):
        return False
    suma = sum(int(d) * p for d, p in zip(ruc[:10], (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)))
    verificador = 11 - suma % 11
    verificador = {10: 0, 11: 1}.get(verificador, verificador)
    return verificador == int(ruc[10])


def dni_valido(dni):
    return bool(re.fullmatch(r'\d{8}', dni or ''))


# ==========================================================================
# Importe en letras (leyenda 1000 obligatoria)
# ==========================================================================
_UNIDADES = ['', 'UNO', 'DOS', 'TRES', 'CUATRO', 'CINCO', 'SEIS', 'SIETE', 'OCHO', 'NUEVE',
             'DIEZ', 'ONCE', 'DOCE', 'TRECE', 'CATORCE', 'QUINCE', 'DIECISEIS', 'DIECISIETE',
             'DIECIOCHO', 'DIECINUEVE', 'VEINTE', 'VEINTIUNO', 'VEINTIDOS', 'VEINTITRES',
             'VEINTICUATRO', 'VEINTICINCO', 'VEINTISEIS', 'VEINTISIETE', 'VEINTIOCHO', 'VEINTINUEVE']
_DECENAS = ['', '', '', 'TREINTA', 'CUARENTA', 'CINCUENTA', 'SESENTA', 'SETENTA', 'OCHENTA', 'NOVENTA']
_CENTENAS = ['', 'CIENTO', 'DOSCIENTOS', 'TRESCIENTOS', 'CUATROCIENTOS', 'QUINIENTOS',
             'SEISCIENTOS', 'SETECIENTOS', 'OCHOCIENTOS', 'NOVECIENTOS']


def _menor_de_mil(n):
    if n == 100:
        return 'CIEN'
    centenas, resto = divmod(n, 100)
    partes = [_CENTENAS[centenas]] if centenas else []
    if resto < 30:
        partes.append(_UNIDADES[resto])
    else:
        decenas, unidades = divmod(resto, 10)
        partes.append(_DECENAS[decenas] + (' Y ' + _UNIDADES[unidades] if unidades else ''))
    return ' '.join(p for p in partes if p)


def _apocope(texto):
    """'VEINTIUNO MIL' -> 'VEINTIUN MIL', 'UNO MILLONES' no ocurre (se trata aparte)."""
    return re.sub(r'UNO$', 'UN', texto)


def _entero_en_letras(n):
    if n == 0:
        return 'CERO'
    millones, resto = divmod(n, 1_000_000)
    miles, unidades = divmod(resto, 1000)
    partes = []
    if millones:
        partes.append('UN MILLON' if millones == 1 else _apocope(_entero_en_letras(millones)) + ' MILLONES')
    if miles:
        partes.append('MIL' if miles == 1 else _apocope(_menor_de_mil(miles)) + ' MIL')
    if unidades:
        partes.append(_menor_de_mil(unidades))
    return ' '.join(partes)


def numero_a_letras(monto):
    """Decimal('1520.50') -> 'MIL QUINIENTOS VEINTE CON 50/100 SOLES'."""
    monto = Decimal(monto).quantize(Decimal('0.01'))
    entero = int(monto)
    centimos = int((monto - entero) * 100)
    return f'{_entero_en_letras(entero)} CON {centimos:02d}/100 SOLES'


# ==========================================================================
# XML UBL 2.1
# ==========================================================================
def _e(padre, etiqueta, texto=None, **atributos):
    prefijo, nombre = etiqueta.split(':') if ':' in etiqueta else ('', etiqueta)
    elemento = etree.SubElement(padre, '{%s}%s' % (NS[prefijo], nombre), {k: str(v) for k, v in atributos.items()})
    if texto is not None:
        elemento.text = str(texto)
    return elemento


def _monto(valor):
    return f'{Decimal(valor):.2f}'


def _impuesto(padre, base, igv, moneda, es_linea):
    total = _e(padre, 'cac:TaxTotal')
    _e(total, 'cbc:TaxAmount', _monto(igv), currencyID=moneda)
    subtotal = _e(total, 'cac:TaxSubtotal')
    _e(subtotal, 'cbc:TaxableAmount', _monto(base), currencyID=moneda)
    _e(subtotal, 'cbc:TaxAmount', _monto(igv), currencyID=moneda)
    categoria = _e(subtotal, 'cac:TaxCategory')
    if es_linea:
        _e(categoria, 'cbc:Percent', '18')
        _e(categoria, 'cbc:TaxExemptionReasonCode', '10')  # catálogo 07: gravado - operación onerosa
    esquema = _e(categoria, 'cac:TaxScheme')
    _e(esquema, 'cbc:ID', '1000')
    _e(esquema, 'cbc:Name', 'IGV')
    _e(esquema, 'cbc:TaxTypeCode', 'VAT')


def nombre_archivo(cab):
    """Nombre oficial: RUC-TIPO-SERIE-CORRELATIVO (sin extensión)."""
    return f'{cab.EmisorRuc}-{cab.TipoComprobante}-{cab.Serie}-{cab.Correlativo}'


def construir_xml(cab, lineas):
    """Arma el comprobante (cabecera de usp_Comprobante_Obtener y sus líneas) en UBL 2.1."""
    moneda = cab.Moneda
    raiz = etree.Element('{%s}Invoice' % NS[''], nsmap={None if k == '' else k: v for k, v in NS.items()})
    # Lugar donde irá la firma digital (signxml lo reemplaza al firmar).
    contenido = _e(_e(_e(raiz, 'ext:UBLExtensions'), 'ext:UBLExtension'), 'ext:ExtensionContent')
    etree.SubElement(contenido, '{%s}Signature' % NS['ds'], Id='placeholder')

    _e(raiz, 'cbc:UBLVersionID', '2.1')
    _e(raiz, 'cbc:CustomizationID', '2.0')
    _e(raiz, 'cbc:ID', f'{cab.Serie}-{cab.Correlativo}')
    _e(raiz, 'cbc:IssueDate', cab.FechaEmision.strftime('%Y-%m-%d'))
    _e(raiz, 'cbc:IssueTime', cab.FechaEmision.strftime('%H:%M:%S'))
    _e(raiz, 'cbc:InvoiceTypeCode', cab.TipoComprobante, listID='0101')  # 0101 = venta interna
    _e(raiz, 'cbc:Note', numero_a_letras(cab.Total), languageLocaleID='1000')
    _e(raiz, 'cbc:DocumentCurrencyCode', moneda)

    # Referencia a la firma
    firma = _e(raiz, 'cac:Signature')
    _e(firma, 'cbc:ID', 'SignatureSP')
    firmante = _e(firma, 'cac:SignatoryParty')
    _e(_e(firmante, 'cac:PartyIdentification'), 'cbc:ID', cab.EmisorRuc)
    _e(_e(firmante, 'cac:PartyName'), 'cbc:Name', cab.EmisorRazonSocial)
    _e(_e(_e(firma, 'cac:DigitalSignatureAttachment'), 'cac:ExternalReference'), 'cbc:URI', '#SignatureSP')

    # Emisor
    emisor = _e(_e(raiz, 'cac:AccountingSupplierParty'), 'cac:Party')
    _e(_e(emisor, 'cac:PartyIdentification'), 'cbc:ID', cab.EmisorRuc, schemeID='6')
    _e(_e(emisor, 'cac:PartyName'), 'cbc:Name', cab.EmisorNombreComercial or cab.EmisorRazonSocial)
    entidad = _e(emisor, 'cac:PartyLegalEntity')
    _e(entidad, 'cbc:RegistrationName', cab.EmisorRazonSocial)
    direccion = _e(entidad, 'cac:RegistrationAddress')
    _e(direccion, 'cbc:ID', cab.EmisorUbigeo)
    _e(direccion, 'cbc:AddressTypeCode', cab.EmisorCodigoEstablecimiento)
    _e(direccion, 'cbc:CityName', cab.EmisorProvincia)
    _e(direccion, 'cbc:CountrySubentity', cab.EmisorDepartamento)
    _e(direccion, 'cbc:District', cab.EmisorDistrito)
    _e(_e(direccion, 'cac:AddressLine'), 'cbc:Line', cab.EmisorDireccion)
    _e(_e(direccion, 'cac:Country'), 'cbc:IdentificationCode', 'PE')

    # Cliente
    cliente = _e(_e(raiz, 'cac:AccountingCustomerParty'), 'cac:Party')
    _e(_e(cliente, 'cac:PartyIdentification'), 'cbc:ID', cab.ClienteNumDoc, schemeID=cab.ClienteTipoDoc)
    _e(_e(cliente, 'cac:PartyLegalEntity'), 'cbc:RegistrationName', cab.ClienteNombre)

    # Forma de pago (la tienda cobra al contado)
    pago = _e(raiz, 'cac:PaymentTerms')
    _e(pago, 'cbc:ID', 'FormaPago')
    _e(pago, 'cbc:PaymentMeansID', 'Contado')

    # Totales
    _impuesto(raiz, cab.OpGravadas, cab.IGV, moneda, es_linea=False)
    totales = _e(raiz, 'cac:LegalMonetaryTotal')
    _e(totales, 'cbc:LineExtensionAmount', _monto(cab.OpGravadas), currencyID=moneda)
    _e(totales, 'cbc:TaxInclusiveAmount', _monto(cab.Total), currencyID=moneda)
    _e(totales, 'cbc:PayableAmount', _monto(cab.Total), currencyID=moneda)

    # Líneas
    for linea in lineas:
        item = _e(raiz, 'cac:InvoiceLine')
        _e(item, 'cbc:ID', linea.Item)
        _e(item, 'cbc:InvoicedQuantity', linea.Cantidad, unitCode=linea.UnidadMedida)
        _e(item, 'cbc:LineExtensionAmount', _monto(linea.ValorVenta), currencyID=moneda)
        precio_con_igv = _e(_e(item, 'cac:PricingReference'), 'cac:AlternativeConditionPrice')
        _e(precio_con_igv, 'cbc:PriceAmount', _monto(linea.PrecioUnitario), currencyID=moneda)
        _e(precio_con_igv, 'cbc:PriceTypeCode', '01')  # catálogo 16: precio unitario con IGV
        _impuesto(item, linea.ValorVenta, linea.IGV, moneda, es_linea=True)
        _e(_e(item, 'cac:Item'), 'cbc:Description', linea.Descripcion)
        _e(_e(item, 'cac:Price'), 'cbc:PriceAmount', f'{Decimal(linea.ValorUnitario):.10f}', currencyID=moneda)
    return raiz


# ==========================================================================
# Firma digital
# ==========================================================================
def _cargar_certificado():
    """Devuelve (clave privada PEM, certificado PEM) desde un .pem (clave + certificado) o un .pfx/.p12."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12

    config = _config()
    ruta = config['certificado']
    if not os.path.isfile(ruta):
        raise RuntimeError(f'No se encontró el certificado digital ({ruta}). '
                           'Para pruebas genera uno con: python generar_certificado_prueba.py')
    with open(ruta, 'rb') as archivo:
        datos = archivo.read()
    clave = config['clave_certificado'].encode() or None
    if ruta.lower().endswith(('.pfx', '.p12')):
        llave, certificado, _ = pkcs12.load_key_and_certificates(datos, clave)
    else:
        from cryptography import x509
        llave = serialization.load_pem_private_key(datos, password=clave)
        certificado = x509.load_pem_x509_certificate(datos)
    llave_pem = llave.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
    return llave_pem, certificado.public_bytes(serialization.Encoding.PEM)


def firmar(raiz):
    """Firma XMLDSig envuelta (RSA-SHA256). Devuelve (bytes del XML firmado, hash/DigestValue)."""
    from signxml import XMLSigner, methods

    llave, certificado = _cargar_certificado()
    firmador = XMLSigner(method=methods.enveloped, signature_algorithm='rsa-sha256', digest_algorithm='sha256',
                         c14n_algorithm='http://www.w3.org/TR/2001/REC-xml-c14n-20010315')
    firmado = firmador.sign(raiz, key=llave, cert=certificado)
    firma = firmado.find('.//{%s}Signature' % NS['ds'])
    firma.set('Id', 'SignatureSP')  # el Id no forma parte de lo firmado
    hash_cpe = firmado.findtext('.//{%s}DigestValue' % NS['ds'])
    return etree.tostring(firmado, xml_declaration=True, encoding='UTF-8'), hash_cpe


# ==========================================================================
# Envío a SUNAT (SOAP sendBill)
# ==========================================================================
def _contexto_tls():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _sobre_soap(ruc, nombre, zip_bytes):
    config = _config()
    usuario = escape(ruc + config['usuario'])
    clave = escape(config['clave'])
    contenido = base64.b64encode(zip_bytes).decode('ascii')
    return (
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:ser="http://service.sunat.gob.pe" '
        'xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
        '<soapenv:Header><wsse:Security><wsse:UsernameToken>'
        f'<wsse:Username>{usuario}</wsse:Username><wsse:Password>{clave}</wsse:Password>'
        '</wsse:UsernameToken></wsse:Security></soapenv:Header>'
        f'<soapenv:Body><ser:sendBill><fileName>{escape(nombre)}.zip</fileName>'
        f'<contentFile>{contenido}</contentFile></ser:sendBill></soapenv:Body></soapenv:Envelope>'
    ).encode('utf-8')


def _codigo_numerico(texto):
    encontrado = re.search(r'(\d{1,4})\s*$', texto or '')
    return int(encontrado.group(1)) if encontrado else None


def enviar(ruc, nombre, xml_firmado):
    """
    Envía el XML firmado a SUNAT y devuelve un dict con:
      estado (ACEPTADO / OBSERVADO / RECHAZADO / ERROR), codigo, descripcion, observaciones, cdr.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archivo_zip:
        archivo_zip.writestr(nombre + '.xml', xml_firmado)
    peticion = urllib.request.Request(
        _config()['url'], data=_sobre_soap(ruc, nombre, buffer.getvalue()),
        headers={'Content-Type': 'text/xml; charset=utf-8', 'SOAPAction': 'urn:sendBill'},
    )
    try:
        respuesta = urllib.request.urlopen(peticion, timeout=40, context=_contexto_tls()).read()
    except urllib.error.HTTPError as error:
        respuesta = error.read()  # SUNAT devuelve los SOAP Fault con HTTP 500
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return {'estado': 'ERROR', 'codigo': None, 'cdr': None, 'observaciones': None,
                'descripcion': f'No se pudo conectar con SUNAT: {getattr(error, "reason", error)}'}

    try:
        documento = etree.fromstring(respuesta)
    except etree.XMLSyntaxError:
        return {'estado': 'ERROR', 'codigo': None, 'cdr': None, 'observaciones': None,
                'descripcion': 'SUNAT devolvió una respuesta que no es XML (servicio no disponible).'}

    falla = documento.find('.//faultstring')
    if falla is not None:
        codigo_falla = documento.findtext('.//faultcode') or ''
        mensaje = documento.findtext('.//detail/message') or documento.findtext('.//message') or ''
        codigo = _codigo_numerico(codigo_falla) or _codigo_numerico(falla.text)
        texto = ' - '.join(t for t in (falla.text, mensaje) if t and not t.strip().isdigit()) or falla.text
        # Códigos 2000-3999: SUNAT RECHAZÓ el comprobante (definitivo).
        # 0100-1999 y otros: error del envío o del servicio; se puede reenviar.
        estado = 'RECHAZADO' if codigo and 2000 <= codigo <= 3999 else 'ERROR'
        return {'estado': estado, 'codigo': str(codigo) if codigo else codigo_falla[:10], 'cdr': None,
                'observaciones': None, 'descripcion': texto}

    nodo_cdr = documento.find('.//applicationResponse')
    if nodo_cdr is None or not nodo_cdr.text:
        return {'estado': 'ERROR', 'codigo': None, 'cdr': None, 'observaciones': None,
                'descripcion': 'SUNAT no devolvió la constancia de recepción (CDR).'}

    cdr_zip = base64.b64decode(nodo_cdr.text)
    with zipfile.ZipFile(io.BytesIO(cdr_zip)) as archivo_zip:
        xml_cdr = next(n for n in archivo_zip.namelist() if n.lower().endswith('.xml'))
        cdr = etree.fromstring(archivo_zip.read(xml_cdr))
    codigo = cdr.findtext('.//cac:DocumentResponse/cac:Response/cbc:ResponseCode', namespaces=NS_CDR) or ''
    descripcion = cdr.findtext('.//cac:DocumentResponse/cac:Response/cbc:Description', namespaces=NS_CDR) or ''
    notas = [n.text for n in cdr.findall('.//cbc:Note', NS_CDR) if n.text]
    numero = _codigo_numerico(codigo)
    if numero == 0:
        estado = 'OBSERVADO' if notas else 'ACEPTADO'
    elif numero is not None and 2000 <= numero <= 3999:
        estado = 'RECHAZADO'
    else:
        estado = 'ERROR'
    return {'estado': estado, 'codigo': codigo, 'descripcion': descripcion,
            'observaciones': ' | '.join(notas) or None, 'cdr': cdr_zip}


# ==========================================================================
# Lectura y emisión
# ==========================================================================
def obtener(cursor, id_comprobante=None, id_venta=None, id_cliente=None):
    """(cabecera, líneas) del comprobante, o (None, []) si no existe o no es del cliente."""
    cursor.execute("EXEC dbo.usp_Comprobante_Obtener @IdComprobante = ?, @IdVenta = ?, @IdCliente = ?",
                   id_comprobante, id_venta, id_cliente)
    cabecera = cursor.fetchone()
    cursor.nextset()
    lineas = cursor.fetchall()
    while cursor.nextset():
        pass
    return (cabecera, lineas) if cabecera else (None, [])


def emitir(conexion, id_comprobante):
    """
    Firma y envía el comprobante a SUNAT y guarda la respuesta (confirma con commit).
    Devuelve la cabecera actualizada. Si el comprobante ya tiene una respuesta
    definitiva de SUNAT, no lo vuelve a enviar.
    """
    cursor = conexion.cursor()
    cabecera, lineas = obtener(cursor, id_comprobante=id_comprobante)
    if cabecera is None:
        raise ValueError(f'No existe el comprobante {id_comprobante}.')
    if cabecera.EstadoSunat in ESTADOS_DEFINITIVOS:
        return cabecera

    xml_firmado = hash_cpe = None
    try:
        xml_firmado, hash_cpe = firmar(construir_xml(cabecera, lineas))
        respuesta = enviar(cabecera.EmisorRuc, nombre_archivo(cabecera), xml_firmado)
    except Exception as error:  # certificado ausente o inválido, XML mal formado, etc.
        respuesta = {'estado': 'ERROR', 'codigo': None, 'cdr': None, 'observaciones': None,
                     'descripcion': f'No se pudo generar o firmar el comprobante: {error}'}

    cursor.execute(
        "EXEC dbo.usp_Comprobante_RegistrarRespuesta @IdComprobante = ?, @EstadoSunat = ?, "
        "@CodigoRespuesta = ?, @DescripcionRespuesta = ?, @Observaciones = ?, @HashCpe = ?, "
        "@XmlFirmado = ?, @CdrZip = ?, @FechaEnvio = ?",
        id_comprobante, respuesta['estado'], (respuesta['codigo'] or None) and str(respuesta['codigo'])[:10],
        (respuesta['descripcion'] or '')[:1000] or None, (respuesta['observaciones'] or '')[:2000] or None,
        hash_cpe, xml_firmado, respuesta['cdr'], datetime.now(),
    )
    resultado_procedimiento(cursor)
    conexion.commit()
    cabecera, lineas = obtener(cursor, id_comprobante=id_comprobante)
    # Con la respuesta de SUNAT ya guardada, se deja el PDF en la carpeta local.
    try:
        import comprobante_pdf
        comprobante_pdf.guardar_pdf(cabecera, lineas)
    except Exception:
        import logging
        logging.getLogger(__name__).exception('No se pudo guardar el PDF de %s', cabecera.NumeroComprobante)
    return cabecera


def texto_estado(cabecera):
    """Frase corta para avisos en pantalla."""
    numero = cabecera.NumeroComprobante
    if cabecera.EstadoSunat == 'ACEPTADO':
        return f'{numero} aceptado por SUNAT.'
    if cabecera.EstadoSunat == 'OBSERVADO':
        return f'{numero} aceptado por SUNAT con observaciones.'
    if cabecera.EstadoSunat == 'RECHAZADO':
        return f'{numero} RECHAZADO por SUNAT: {cabecera.DescripcionRespuesta}'
    if cabecera.EstadoSunat == 'ERROR':
        return f'{numero} pendiente de envío a SUNAT (se reintentará).'
    return f'{numero} pendiente de envío a SUNAT.'


# ==========================================================================
# Pantallas (comunes a la tienda y al panel)
# ==========================================================================
def pagina_impresa(cursor, id_venta, id_cliente=None):
    """HTML de la representación impresa, o None si no hay comprobante (o no es del cliente)."""
    from flask import render_template

    cabecera, lineas = obtener(cursor, id_venta=id_venta, id_cliente=id_cliente)
    if cabecera is None:
        return None
    return render_template('comprobante_impreso.html', c=cabecera, lineas=lineas,
                           titulo=TIPOS_COMPROBANTE[cabecera.TipoComprobante],
                           tipo_documento=TIPOS_DOCUMENTO.get(cabecera.ClienteTipoDoc, 'DOCUMENTO'),
                           en_letras=numero_a_letras(cabecera.Total), beta=es_entorno_beta())


def respuesta_archivo(cursor, id_venta, tipo, id_cliente=None):
    """Descarga del XML firmado o del CDR (tipo 'xml' o 'cdr'), o None si no existe."""
    from flask import Response

    tipo = (tipo or '').upper()
    if tipo not in ('XML', 'CDR'):
        return None
    cursor.execute("EXEC dbo.usp_Comprobante_ObtenerArchivo @IdVenta = ?, @Tipo = ?, @IdCliente = ?",
                   id_venta, tipo, id_cliente)
    fila = cursor.fetchone()
    while cursor.nextset():
        pass
    if fila is None:
        return None
    return Response(bytes(fila.Contenido),
                    mimetype='application/zip' if tipo == 'CDR' else 'application/xml',
                    headers={'Content-Disposition': f'attachment; filename="{fila.NombreArchivo}"'})
