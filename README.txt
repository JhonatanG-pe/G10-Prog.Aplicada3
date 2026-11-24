===============================================================================
  SISTEMA DE CARRITO DE COMPRAS
  Panel interno (Administrador / Operador) + Tienda en línea (Cliente)
  Python 3.12 + Flask + SQL Server
===============================================================================

CONTENIDO DEL PROYECTO
----------------------
  app.py              Punto de entrada: arranca el sistema.
  db.py               Capa de acceso a datos: una conexión a SQL Server por petición,
                      cerrada siempre al terminar (lee los datos del archivo .env).
  paginacion.py       Paginación de listados (SQL Server devuelve solo la página pedida).
  seguridad.py        Reglas de contraseñas (HASHBYTES + salt).
  facturacion.py      Facturación electrónica SUNAT: arma el XML UBL 2.1 de la boleta o
                      factura, lo firma, lo envía a SUNAT y guarda la respuesta (CDR).
  comprobante_pdf.py  Genera el PDF de la boleta o factura y lo guarda en "comprobantes/".
  generar_certificado_prueba.py  Crea el certificado digital de PRUEBA (punto 2).
  sql/                Scripts de la base de datos (ya aplicados en el hosting):
                        indices_escalabilidad.sql  índices y claves.
                        facturacion_electronica.sql tablas Empresa, SerieComprobante,
                                                   Comprobante y Comprobante_Detalle.
                        stored_procedures.sql      procedimientos almacenados (checkout con
                                                   comprobante, pedidos, compras, reportes,
                                                   logins y comprobantes SUNAT).
                      Orden: facturacion_electronica.sql y luego stored_procedures.sql.
  crear_usuario.py    Script de consola para crear un usuario interno.
  routes/             Lógica del panel interno y de la tienda.
  templates/          Pantallas (HTML).
  almacen_fotos.py    Sube las fotos de productos al hosting (carpeta Fotos del sitio web,
                      por FTP cifrado): así todas las PC ven las mismas fotos.
  migrar_fotos_hosting.py  Pasa al hosting las fotos antiguas guardadas en la PC.
  docs/               Distribución de la revisión, criterio de terminado y seguridad (PDF).
  .env.example        Plantilla del archivo de configuración.
  requirements.txt    Librerías de Python (versiones exactas).


-------------------------------------------------------------------------------
1. QUÉ INSTALAR (una sola vez)
-------------------------------------------------------------------------------
  a) Python 3.12 (64 bits): https://www.python.org/downloads/
     Al instalar, marca la casilla "Add Python to PATH".
     (Python 3.14 también funciona; si alguna instalación falla, usa 3.12.)

  b) ODBC Driver 17 o 18 for SQL Server, versión x64 (de Microsoft).
     Busca en Google: "ODBC Driver 17 for SQL Server download".
     Para ver cuál tienes instalado, en PowerShell:
        Get-OdbcDriver | Where-Object Name -like "*SQL Server*" | Select-Object Name, Platform
     Sirven SOLO el 17 o el 18 (los antiguos "SQL Server" o "Native Client" no).


-------------------------------------------------------------------------------
2. PREPARAR EL PROYECTO (una sola vez)
-------------------------------------------------------------------------------
  Descomprime el ZIP, abre PowerShell dentro de la carpeta del proyecto y ejecuta:

     python -m venv venv
     venv\Scripts\Activate.ps1
     pip install -r requirements.txt

  Debe aparecer "(venv)" al inicio de la línea de comandos.

  Luego crea tu archivo de configuración:
     1. Copia ".env.example" y renombra la copia como ".env".
     2. Completa DB_SERVER, DB_NAME, DB_USER y DB_PASSWORD con los datos que te
        pase Ricardo por mensaje privado.
     3. En SECRET_KEY pon un texto largo y aleatorio. Para generarlo:
           python -c "import secrets; print(secrets.token_hex(32))"
     4. Si tienes el driver 18, agrega:  DB_DRIVER=ODBC Driver 18 for SQL Server
     5. Completa FTP_USUARIO y FTP_CLAVE (fotos de productos en el hosting) con
        los datos que te pase Ricardo por mensaje privado.

  Certificado digital de prueba (para firmar boletas y facturas):
     python generar_certificado_prueba.py
  Crea la carpeta "certificados". Contiene una clave privada: no la compartas.
  Sin certificado la compra igual se registra, pero el comprobante queda
  "Pendiente de envío" a SUNAT.

  IMPORTANTE: el archivo .env tiene contraseñas. No lo compartas ni lo subas a internet.


-------------------------------------------------------------------------------
3. LEVANTAR EL SISTEMA (cada vez que lo uses)
-------------------------------------------------------------------------------
     venv\Scripts\Activate.ps1
     python app.py

  Abre en el navegador:
     Tienda (clientes) ......... http://127.0.0.1:5000
     Panel interno ............. http://127.0.0.1:5000/admin/login

  Para detener el sistema: Ctrl + C en la terminal.
  Si cambias código, detén y vuelve a ejecutar "python app.py"
  (o pon FLASK_DEBUG=1 en tu .env para que se recargue solo).


-------------------------------------------------------------------------------
4. USUARIOS PARA PROBAR
-------------------------------------------------------------------------------
  - Panel interno: pídele a Ricardo un usuario (lo crea en Usuarios). Al entrar,
    elige el perfil con el que vas a trabajar (Administrador u Operador).
  - Tienda: crea tu propia cuenta de cliente con "Crear cuenta".
  - Usa datos de prueba propios para no pisar los de los demás
    (por ejemplo "Categoría Bryan" o el correo "jonathan.prueba@...").
  - No borres ni modifiques datos reales que no hayas creado tú.


-------------------------------------------------------------------------------
5. BOLETAS Y FACTURAS ELECTRÓNICAS (SUNAT, modo prueba)
-------------------------------------------------------------------------------
  - Al finalizar la compra el cliente elige Boleta (DNI opcional; obligatorio si
    el total supera S/ 700) o Factura (RUC válido + razón social).
  - El sistema emite el comprobante (serie B001 o F001), lo firma y lo envía al
    entorno de PRUEBAS (beta) de SUNAT. La respuesta aparece en el detalle del
    pedido: Aceptado, Aceptado con observaciones, Rechazado o Pendiente de envío.
  - En el detalle del pedido: "Ver / imprimir", "Descargar PDF", "Descargar XML" y
    "Descargar CDR" (constancia de recepción de SUNAT).
  - PDF: cada vez que SUNAT responde y cada vez que se usa "Descargar PDF", el PDF
    (con código QR y hash) se guarda en la carpeta "comprobantes" del proyecto, con
    el nombre oficial, por ejemplo 20000000001-03-B001-1.pdf. En el panel, si quedó pendiente (por
    ejemplo, sin internet), el botón "Enviar a SUNAT" lo reintenta.
  - Datos del emisor: tabla Empresa (en beta, el RUC de pruebas 20000000001).
  - Los comprobantes de prueba NO tienen validez tributaria.


-------------------------------------------------------------------------------
6. PROBLEMAS FRECUENTES
-------------------------------------------------------------------------------
  * "No se puede cargar el archivo ...\Activate.ps1 porque la ejecución de
    scripts está deshabilitada"
      Ejecuta una vez en PowerShell y responde "S":
         Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
      (O usa la terminal CMD y activa con:  venv\Scripts\activate.bat)

  * "IM002 ... No se encuentra el nombre del origen de datos"
    o "No está instalado el driver ..."
      Falta el ODBC Driver 17/18 x64, o el .env indica un driver que no tienes.
      Revisa el punto 1.b y la línea DB_DRIVER de tu .env.

  * "KeyError: 'SECRET_KEY'" o "KeyError: 'DB_SERVER'"
      Falta el archivo .env o le falta esa línea (punto 2).

  * "ModuleNotFoundError: No module named 'lxml'" (o 'signxml', 'fpdf', 'segno')
      Faltan las librerías nuevas: con el venv activo, ejecuta otra vez
         pip install -r requirements.txt

  * El comprobante queda "Pendiente de envío" con el mensaje
    "No se encontró el certificado digital"
      Ejecuta:  python generar_certificado_prueba.py
      y en el panel usa "Enviar a SUNAT" en ese pedido.

  * Al guardar un producto con foto sale "No está configurado el almacenamiento
    de fotos en el hosting" o "rechazó el usuario o la contraseña FTP"
      Revisa en tu .env las líneas FTP_SERVIDOR, FTP_USUARIO, FTP_CLAVE, FTP_CARPETA
      y FOTOS_URL_PUBLICA (punto 2). Para probar la conexión:
         python almacen_fotos.py

  * La foto de un producto antiguo se ve rota
      Antes las fotos se guardaban en la PC de quien las subía. Quien la subió
      puede pasarla al hosting con:  python migrar_fotos_hosting.py --aplicar
      o cualquiera puede volver a subirla desde "Editar producto".


-------------------------------------------------------------------------------
7. CÓMO REVISAR LAS HISTORIAS DE USUARIO
-------------------------------------------------------------------------------
  - Tus historias asignadas están en docs/Distribucion_revision_HU.pdf
  - Cada tarjeta de Trello (tablero "Historias de Usuario") tiene sus criterios
    de aceptación y la checklist "Criterio de terminado" (docs/Criterio_de_terminado.pdf).
  - Prueba cada criterio, también con datos inválidos, guarda capturas como
    evidencia y marca la checklist. Si algo falla, coméntalo en la tarjeta.
