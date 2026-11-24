"""
PDF de la boleta o factura electrónica (representación impresa) y su copia local.

  * generar_pdf()    arma el PDF A4: emisor, recuadro RUC/tipo/número, cliente, detalle,
                     totales, importe en letras, código QR (formato SUNAT), hash y estado.
  * guardar_pdf()    lo guarda en la carpeta local (por defecto "comprobantes/",
                     configurable con SUNAT_CARPETA_PDF en el .env) con el nombre oficial
                     RUC-TIPO-SERIE-NUMERO.pdf. Si ya existía, lo reemplaza por el actualizado.
  * respuesta_pdf()  lo genera, lo guarda y lo entrega al navegador para descargar.

facturacion.emitir() también guarda el PDF apenas SUNAT responde.
"""
import io
import os

from facturacion import (CARPETA_PROYECTO, TIPOS_COMPROBANTE, TIPOS_DOCUMENTO, _monto, es_entorno_beta,
                         nombre_archivo, numero_a_letras, obtener)

ESTADOS = {'ACEPTADO': 'Aceptado por SUNAT', 'OBSERVADO': 'Aceptado por SUNAT con observaciones',
           'RECHAZADO': 'RECHAZADO por SUNAT', 'ERROR': 'Pendiente de envío a SUNAT',
           'PENDIENTE': 'Pendiente de envío a SUNAT'}


def carpeta_pdf():
    """Carpeta local de los PDF (SUNAT_CARPETA_PDF en el .env; por defecto 'comprobantes')."""
    carpeta = os.environ.get('SUNAT_CARPETA_PDF', '').strip() or 'comprobantes'
    if not os.path.isabs(carpeta):
        carpeta = os.path.join(CARPETA_PROYECTO, carpeta)
    return carpeta


def ruta_pdf(cab):
    return os.path.join(carpeta_pdf(), nombre_archivo(cab) + '.pdf')


def texto_qr(cab):
    """Contenido del QR que exige SUNAT: RUC|TIPO|SERIE|NUMERO|IGV|TOTAL|FECHA|TIPO DOC|NUM DOC|HASH|"""
    return '|'.join([cab.EmisorRuc, cab.TipoComprobante, cab.Serie, str(cab.Correlativo), _monto(cab.IGV),
                     _monto(cab.Total), cab.FechaEmision.strftime('%Y-%m-%d'), cab.ClienteTipoDoc,
                     cab.ClienteNumDoc, cab.HashCpe or '']) + '|'


def _t(texto):
    """Las fuentes estándar del PDF solo admiten Latin-1 (incluye tildes y ñ)."""
    return str(texto if texto is not None else '').encode('latin-1', 'replace').decode('latin-1')


def generar_pdf(cab, lineas):
    """Devuelve los bytes del PDF del comprobante (cabecera y líneas de usp_Comprobante_Obtener)."""
    import segno
    from fpdf import FPDF

    titulo = TIPOS_COMPROBANTE[cab.TipoComprobante]
    pdf = FPDF(format='A4', unit='mm')
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_title(_t(f'{titulo} {cab.NumeroComprobante}'))
    pdf.set_author(_t(cab.EmisorRazonSocial))
    pdf.add_page()

    if es_entorno_beta():
        pdf.set_fill_color(255, 243, 205)
        pdf.set_font('Helvetica', 'B', 8)
        pdf.cell(0, 6, 'COMPROBANTE DE PRUEBA (SUNAT BETA): NO TIENE VALIDEZ TRIBUTARIA', border=1, fill=True,
                 align='C', new_x='LMARGIN', new_y='NEXT')
        pdf.ln(3)

    # Emisor (izquierda) y recuadro con RUC, tipo y número (derecha)
    y = pdf.get_y()
    pdf.set_font('Helvetica', 'B', 14)
    pdf.multi_cell(112, 7, _t(cab.EmisorNombreComercial or cab.EmisorRazonSocial), new_x='LMARGIN', new_y='NEXT')
    pdf.set_font('Helvetica', '', 9)
    pdf.multi_cell(112, 5, _t(cab.EmisorRazonSocial), new_x='LMARGIN', new_y='NEXT')
    pdf.multi_cell(112, 5, _t(f'{cab.EmisorDireccion} - {cab.EmisorDistrito} - {cab.EmisorProvincia} - '
                              f'{cab.EmisorDepartamento}'), new_x='LMARGIN', new_y='NEXT')
    fin_emisor = pdf.get_y()
    pdf.set_line_width(0.6)
    pdf.rect(128, y, 72, 27)
    pdf.set_line_width(0.2)
    pdf.set_xy(128, y + 1)
    pdf.set_font('Helvetica', 'B', 11)
    pdf.cell(72, 8, _t(f'R.U.C. {cab.EmisorRuc}'), align='C', new_x='LEFT', new_y='NEXT')
    pdf.set_font('Helvetica', 'B', 10)
    pdf.cell(72, 8, _t(titulo), align='C', new_x='LEFT', new_y='NEXT')
    pdf.set_font('Helvetica', 'B', 11)
    pdf.cell(72, 8, _t(f'{cab.Serie} - {cab.Correlativo:08d}'), align='C')
    pdf.set_xy(pdf.l_margin, max(fin_emisor, y + 27) + 6)

    # Datos del cliente
    datos = [('Fecha de emisión', cab.FechaEmision.strftime('%d/%m/%Y %H:%M:%S')),
             ('Señor(es)' if cab.TipoComprobante == '01' else 'Cliente', cab.ClienteNombre),
             (TIPOS_DOCUMENTO.get(cab.ClienteTipoDoc, 'DOCUMENTO'), cab.ClienteNumDoc)]
    if cab.ClienteDireccion:
        datos.append(('Dirección', cab.ClienteDireccion))
    datos += [('Moneda', 'SOLES'), ('Forma de pago', 'Contado'), ('Pedido N°', cab.IdVenta)]
    for etiqueta, valor in datos:
        pdf.set_font('Helvetica', 'B', 9)
        pdf.cell(40, 5, _t(etiqueta + ':'))
        pdf.set_font('Helvetica', '', 9)
        pdf.multi_cell(0, 5, _t(valor), new_x='LMARGIN', new_y='NEXT')
    pdf.ln(3)

    # Detalle
    anchos = (10, 13, 17, 80, 23, 23, 24)
    def encabezado():
        pdf.set_font('Helvetica', 'B', 8)
        pdf.set_fill_color(235, 235, 235)
        for ancho, columna in zip(anchos, ('Ítem', 'Cant.', 'Unid.', 'Descripción', 'V. unit.', 'P. unit.', 'Importe')):
            pdf.cell(ancho, 7, _t(columna), border=1, fill=True, align='C')
        pdf.ln()
        pdf.set_font('Helvetica', '', 8)
    encabezado()
    for linea in lineas:
        descripcion = _t(linea.Descripcion)
        renglones = pdf.multi_cell(anchos[3] - 2, 5, descripcion, dry_run=True, output='LINES')
        alto = 5 * max(1, len(renglones))
        if pdf.get_y() + alto > pdf.h - pdf.b_margin:
            pdf.add_page()
            encabezado()
        x, y = pdf.l_margin, pdf.get_y()
        valores = (str(linea.Item), str(linea.Cantidad), 'UNIDAD', None, f'{linea.ValorUnitario:,.2f}',
                   f'{linea.PrecioUnitario:,.2f}', f'{linea.Importe:,.2f}')
        for ancho, valor in zip(anchos, valores):
            pdf.rect(x, y, ancho, alto)
            pdf.set_xy(x, y)
            if valor is None:
                pdf.multi_cell(ancho, 5, descripcion, new_x='RIGHT', new_y='TOP')
            else:
                pdf.cell(ancho, alto, valor, align='C' if ancho in (10, 17) else 'R')
            x += ancho
        pdf.set_xy(pdf.l_margin, y + alto)

    # Totales e importe en letras
    pdf.ln(3)
    for etiqueta, valor, negrita in (('Op. gravadas', cab.OpGravadas, False), ('IGV (18%)', cab.IGV, False),
                                     ('Importe total', cab.Total, True)):
        pdf.set_font('Helvetica', 'B' if negrita else '', 9)
        pdf.cell(150, 6, etiqueta, align='R')
        pdf.cell(40, 6, f'S/ {valor:,.2f}', align='R', new_x='LMARGIN', new_y='NEXT')
    pdf.ln(2)
    pdf.set_font('Helvetica', 'B', 9)
    pdf.cell(12, 5, 'SON:')
    pdf.set_font('Helvetica', '', 9)
    pdf.multi_cell(0, 5, _t(numero_a_letras(cab.Total)), new_x='LMARGIN', new_y='NEXT')

    # Código QR + leyenda, estado ante SUNAT y hash
    pdf.ln(4)
    if pdf.get_y() + 34 > pdf.h - pdf.b_margin:
        pdf.add_page()
    y = pdf.get_y()
    imagen_qr = io.BytesIO()
    segno.make(texto_qr(cab), error='m').save(imagen_qr, kind='png', scale=6, border=1)
    imagen_qr.seek(0)
    pdf.image(imagen_qr, x=pdf.l_margin, y=y, w=32, h=32)
    pie = [f'Representación impresa de la {titulo}.',
           f'Estado: {ESTADOS.get(cab.EstadoSunat, cab.EstadoSunat)}'
           + (f' - {cab.DescripcionRespuesta}' if cab.DescripcionRespuesta else '')]
    if cab.HashCpe:
        pie.append(f'Resumen (hash): {cab.HashCpe}')
    pdf.set_font('Helvetica', '', 8)
    pdf.set_y(y + 2)
    for texto in pie:
        pdf.set_x(pdf.l_margin + 36)
        pdf.multi_cell(0, 4.5, _t(texto), new_x='LMARGIN', new_y='NEXT')
    pdf.set_y(max(pdf.get_y(), y + 33))
    return bytes(pdf.output())


def guardar_pdf(cab, lineas):
    """Genera el PDF y lo guarda en la carpeta local. Devuelve (ruta del archivo, bytes del PDF)."""
    contenido = generar_pdf(cab, lineas)
    os.makedirs(carpeta_pdf(), exist_ok=True)
    ruta = ruta_pdf(cab)
    temporal = ruta + '.tmp'
    with open(temporal, 'wb') as archivo:
        archivo.write(contenido)
    os.replace(temporal, ruta)  # si el PDF está abierto en otro programa falla aquí, sin dañar el anterior
    return ruta, contenido


def respuesta_pdf(cursor, id_venta, id_cliente=None):
    """Genera el PDF actualizado, guarda la copia local y lo entrega para descargar (None si no existe)."""
    from flask import Response, current_app

    cabecera, lineas = obtener(cursor, id_venta=id_venta, id_cliente=id_cliente)
    if cabecera is None:
        return None
    try:
        _, contenido = guardar_pdf(cabecera, lineas)
    except OSError:
        # Sin permiso de escritura o archivo bloqueado: se entrega igual, sin copia local.
        current_app.logger.exception('No se pudo guardar el PDF de %s', cabecera.NumeroComprobante)
        contenido = generar_pdf(cabecera, lineas)
    return Response(contenido, mimetype='application/pdf',
                    headers={'Content-Disposition': f'attachment; filename="{nombre_archivo(cabecera)}.pdf"'})
