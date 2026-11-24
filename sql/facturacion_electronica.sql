/* =============================================================================
   Facturación electrónica SUNAT (Boleta 03 / Factura 01) - Tablas
   Base de datos: db_a82a66_carrito (SQL Server 2016 o superior)

   ORDEN DE EJECUCIÓN (SSMS: abrir cada archivo y F5):
     1. sql/facturacion_electronica.sql   (este archivo: tablas y datos iniciales)
     2. sql/stored_procedures.sql         (procedimientos, incluidos los de comprobantes)

   Es idempotente: cada objeto se crea solo si todavía no existe, así que se puede
   ejecutar más de una vez. No modifica ni borra los datos existentes.

   Tablas:
     Empresa              Datos del EMISOR (una sola fila). Viene con el RUC de
                          pruebas de SUNAT (20000000001), obligatorio en el entorno
                          beta. En producción se cambia por el RUC real.
     SerieComprobante     Series y último correlativo usado: F001 (facturas) y
                          B001 (boletas). El correlativo lo asigna usp_Venta_Registrar.
     Comprobante          Cabecera del comprobante de cada venta: cliente, totales,
                          estado ante SUNAT, XML firmado y CDR (constancia de recepción).
     Comprobante_Detalle  Líneas del comprobante: valor unitario sin IGV, valor de
                          venta e IGV de cada producto (copia fija al momento de emitir).

   Las credenciales SOL y el certificado digital NO se guardan en la base de datos:
   van en el archivo .env (ver .env.example).
   ============================================================================= */

/* ---------- Empresa (emisor) ---------- */
IF OBJECT_ID('dbo.Empresa', 'U') IS NULL
    CREATE TABLE dbo.Empresa (
        IdEmpresa             INT          NOT NULL CONSTRAINT PK_Empresa PRIMARY KEY,
        Ruc                   CHAR(11)     NOT NULL,
        RazonSocial           VARCHAR(200) NOT NULL,
        NombreComercial       VARCHAR(200) NULL,
        Direccion             VARCHAR(300) NOT NULL,
        Ubigeo                CHAR(6)      NOT NULL,
        Departamento          VARCHAR(50)  NOT NULL,
        Provincia             VARCHAR(50)  NOT NULL,
        Distrito              VARCHAR(50)  NOT NULL,
        CodigoEstablecimiento CHAR(4)      NOT NULL CONSTRAINT DF_Empresa_CodEstab DEFAULT '0000',
        CONSTRAINT CK_Empresa_UnaFila CHECK (IdEmpresa = 1)
    );
GO

IF NOT EXISTS (SELECT 1 FROM dbo.Empresa)
    INSERT INTO dbo.Empresa (IdEmpresa, Ruc, RazonSocial, NombreComercial, Direccion, Ubigeo,
                             Departamento, Provincia, Distrito)
    VALUES (1, '20000000001', 'TIENDA DE ELECTRONICOS S.A.C.', 'TIENDA DE ELECTRONICOS',
            'AV. UNIVERSITARIA 1234', '150101', 'LIMA', 'LIMA', 'LIMA');
GO

/* ---------- Series y correlativos ---------- */
IF OBJECT_ID('dbo.SerieComprobante', 'U') IS NULL
    CREATE TABLE dbo.SerieComprobante (
        IdSerie           INT IDENTITY(1, 1) NOT NULL CONSTRAINT PK_SerieComprobante PRIMARY KEY,
        TipoComprobante   CHAR(2) NOT NULL,      -- 01 = Factura, 03 = Boleta (catálogo 01 SUNAT)
        Serie             CHAR(4) NOT NULL,
        UltimoCorrelativo INT     NOT NULL CONSTRAINT DF_Serie_Ultimo DEFAULT 0,
        Activo            BIT     NOT NULL CONSTRAINT DF_Serie_Activo DEFAULT 1,
        CONSTRAINT UQ_SerieComprobante UNIQUE (TipoComprobante, Serie),
        CONSTRAINT CK_Serie_Tipo CHECK (TipoComprobante IN ('01', '03')),
        CONSTRAINT CK_Serie_Formato CHECK ((TipoComprobante = '01' AND Serie LIKE 'F[0-9A-Z][0-9A-Z][0-9A-Z]')
                                        OR (TipoComprobante = '03' AND Serie LIKE 'B[0-9A-Z][0-9A-Z][0-9A-Z]'))
    );
GO

IF NOT EXISTS (SELECT 1 FROM dbo.SerieComprobante WHERE TipoComprobante = '01' AND Serie = 'F001')
    INSERT INTO dbo.SerieComprobante (TipoComprobante, Serie) VALUES ('01', 'F001');
IF NOT EXISTS (SELECT 1 FROM dbo.SerieComprobante WHERE TipoComprobante = '03' AND Serie = 'B001')
    INSERT INTO dbo.SerieComprobante (TipoComprobante, Serie) VALUES ('03', 'B001');
GO

/* ---------- Comprobante (cabecera) ---------- */
IF OBJECT_ID('dbo.Comprobante', 'U') IS NULL
    CREATE TABLE dbo.Comprobante (
        IdComprobante        INT IDENTITY(1, 1) NOT NULL CONSTRAINT PK_Comprobante PRIMARY KEY,
        IdVenta              INT            NOT NULL CONSTRAINT FK_Comprobante_Venta REFERENCES dbo.Venta (IdVenta),
        TipoComprobante      CHAR(2)        NOT NULL,
        Serie                CHAR(4)        NOT NULL,
        Correlativo          INT            NOT NULL,
        FechaEmision         DATETIME       NOT NULL,
        Moneda               CHAR(3)        NOT NULL CONSTRAINT DF_Comprobante_Moneda DEFAULT 'PEN',
        ClienteTipoDoc       CHAR(1)        NOT NULL,   -- 6 = RUC, 1 = DNI, 0 = sin documento (catálogo 06)
        ClienteNumDoc        VARCHAR(15)    NOT NULL,
        ClienteNombre        VARCHAR(200)   NOT NULL,
        ClienteDireccion     VARCHAR(300)   NULL,
        OpGravadas           DECIMAL(12, 2) NOT NULL,   -- valor de venta sin IGV
        IGV                  DECIMAL(12, 2) NOT NULL,
        Total                DECIMAL(12, 2) NOT NULL,
        EstadoSunat          VARCHAR(15)    NOT NULL CONSTRAINT DF_Comprobante_Estado DEFAULT 'PENDIENTE',
        CodigoRespuesta      VARCHAR(10)    NULL,
        DescripcionRespuesta VARCHAR(1000)  NULL,
        Observaciones        VARCHAR(2000)  NULL,
        HashCpe              VARCHAR(100)   NULL,       -- DigestValue de la firma (va en la representación impresa)
        XmlFirmado           VARBINARY(MAX) NULL,
        CdrZip               VARBINARY(MAX) NULL,
        FechaEnvio           DATETIME       NULL,
        Intentos             INT            NOT NULL CONSTRAINT DF_Comprobante_Intentos DEFAULT 0,
        CONSTRAINT UQ_Comprobante_Venta UNIQUE (IdVenta),
        CONSTRAINT UQ_Comprobante_Numero UNIQUE (TipoComprobante, Serie, Correlativo),
        CONSTRAINT CK_Comprobante_Tipo CHECK (TipoComprobante IN ('01', '03')),
        CONSTRAINT CK_Comprobante_Estado CHECK (EstadoSunat IN ('PENDIENTE', 'ACEPTADO', 'OBSERVADO', 'RECHAZADO', 'ERROR')),
        CONSTRAINT CK_Comprobante_Totales CHECK (Total = OpGravadas + IGV)
    );
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_Comprobante_EstadoSunat')
    CREATE NONCLUSTERED INDEX IX_Comprobante_EstadoSunat ON dbo.Comprobante (EstadoSunat);
GO

/* ---------- Comprobante (detalle) ---------- */
IF OBJECT_ID('dbo.Comprobante_Detalle', 'U') IS NULL
    CREATE TABLE dbo.Comprobante_Detalle (
        IdComprobanteDetalle INT IDENTITY(1, 1) NOT NULL CONSTRAINT PK_Comprobante_Detalle PRIMARY KEY,
        IdComprobante        INT            NOT NULL CONSTRAINT FK_CompDetalle_Comprobante REFERENCES dbo.Comprobante (IdComprobante),
        Item                 INT            NOT NULL,
        IdProducto           INT            NOT NULL CONSTRAINT FK_CompDetalle_Producto REFERENCES dbo.Producto (IdProducto),
        Descripcion          VARCHAR(500)   NOT NULL,
        Cantidad             INT            NOT NULL,
        UnidadMedida         CHAR(3)        NOT NULL CONSTRAINT DF_CompDetalle_Unidad DEFAULT 'NIU',  -- NIU = unidad (bienes)
        ValorUnitario        DECIMAL(20, 10) NOT NULL,  -- precio sin IGV
        PrecioUnitario       DECIMAL(10, 2) NOT NULL,   -- precio con IGV (el del catálogo)
        ValorVenta           DECIMAL(12, 2) NOT NULL,   -- cantidad x valor unitario, sin IGV
        IGV                  DECIMAL(12, 2) NOT NULL,
        CONSTRAINT UQ_Comprobante_Detalle_Item UNIQUE (IdComprobante, Item)
    );
GO

PRINT 'Tablas de facturación electrónica creadas (o ya existían).';
