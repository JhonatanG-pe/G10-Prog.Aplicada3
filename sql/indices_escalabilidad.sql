/* =============================================================================
   Índices y claves para escalabilidad - Sistema de carrito de compras
   Base de datos: db_a82a66_carrito (SQL Server)

   Qué hace:
     1. Claves primarias en las tablas de asignación que no tenían ninguna
        (impiden asignar dos veces el mismo perfil o la misma opción de menú).
     2. Índices ÚNICOS: la base de datos garantiza por sí misma que no se repitan
        el correo y el DNI del usuario interno, el RUC del proveedor ni un mismo
        producto dos veces en el carrito de un cliente (antes solo lo controlaba
        el código Python).
     3. Índices en las columnas por las que más se busca, filtra u ordena
        (claves foráneas, estados y fechas), para que las consultas sigan siendo
        rápidas cuando las tablas crezcan.

   Es idempotente: cada objeto se crea solo si todavía no existe, así que se
   puede ejecutar más de una vez sin errores. No modifica ni borra datos.
   ============================================================================= */
SET XACT_ABORT ON;
BEGIN TRANSACTION;

/* ---------- 1. Claves primarias faltantes ---------- */
IF NOT EXISTS (SELECT 1 FROM sys.key_constraints WHERE name = 'PK_Usuario_Perfiles')
    ALTER TABLE dbo.Usuario_Perfiles
        ADD CONSTRAINT PK_Usuario_Perfiles PRIMARY KEY CLUSTERED (IdUsuario, IdPerfil);

IF NOT EXISTS (SELECT 1 FROM sys.key_constraints WHERE name = 'PK_OpcionesMenu_Perfiles')
    ALTER TABLE dbo.OpcionesMenu_Perfiles
        ADD CONSTRAINT PK_OpcionesMenu_Perfiles PRIMARY KEY CLUSTERED (IdPerfil, IdOpcionMenu);

/* ---------- 2. Unicidad garantizada por la base de datos ---------- */
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UQ_Usuario_CorreoElectronico')
    CREATE UNIQUE NONCLUSTERED INDEX UQ_Usuario_CorreoElectronico ON dbo.Usuario (CorreoElectronico);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UQ_Usuario_DNI')
    CREATE UNIQUE NONCLUSTERED INDEX UQ_Usuario_DNI ON dbo.Usuario (DNI);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UQ_Proveedor_NumDocIdentidad')
    CREATE UNIQUE NONCLUSTERED INDEX UQ_Proveedor_NumDocIdentidad ON dbo.Proveedor (NumDocIdentidad)
        WHERE NumDocIdentidad IS NOT NULL;

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UQ_Carrito_Cliente_Producto')
    CREATE UNIQUE NONCLUSTERED INDEX UQ_Carrito_Cliente_Producto ON dbo.Carrito (IdCliente, IdProducto);

/* ---------- 3. Índices de búsqueda ---------- */
-- Tienda y listado de productos: solo activos, ordenados por nombre.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_Producto_Activo_Nombre')
    CREATE NONCLUSTERED INDEX IX_Producto_Activo_Nombre ON dbo.Producto (Activo, Nombre);
-- Al anular una categoría o una marca se cuentan sus productos.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_Producto_IdCategoria')
    CREATE NONCLUSTERED INDEX IX_Producto_IdCategoria ON dbo.Producto (IdCategoria);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_Producto_IdMarca')
    CREATE NONCLUSTERED INDEX IX_Producto_IdMarca ON dbo.Producto (IdMarca);

-- "Mis pedidos" del cliente, del más reciente al más antiguo.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_Venta_IdCliente_FechaVenta')
    CREATE NONCLUSTERED INDEX IX_Venta_IdCliente_FechaVenta ON dbo.Venta (IdCliente, FechaVenta DESC);
-- Gestión de pedidos filtrada por estado.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_Venta_Estado_FechaVenta')
    CREATE NONCLUSTERED INDEX IX_Venta_Estado_FechaVenta ON dbo.Venta (Estado, FechaVenta DESC);
-- Listado general de pedidos y reporte de ventas por rango de fechas.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_Venta_FechaVenta')
    CREATE NONCLUSTERED INDEX IX_Venta_FechaVenta ON dbo.Venta (FechaVenta DESC);

-- Detalle de cada pedido y ranking de productos más vendidos.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_Detalle_Venta_IdVenta')
    CREATE NONCLUSTERED INDEX IX_Detalle_Venta_IdVenta ON dbo.Detalle_Venta (IdVenta);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_Detalle_Venta_IdProducto')
    CREATE NONCLUSTERED INDEX IX_Detalle_Venta_IdProducto ON dbo.Detalle_Venta (IdProducto) INCLUDE (Cantidad, Precio);

-- Historial de compras filtrado por proveedor y/o fechas, y detalle de cada compra.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_Compra_IdProveedor_Fecha')
    CREATE NONCLUSTERED INDEX IX_Compra_IdProveedor_Fecha ON dbo.Compra (IdProveedor, Fecha DESC);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_Compra_Fecha')
    CREATE NONCLUSTERED INDEX IX_Compra_Fecha ON dbo.Compra (Fecha DESC);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_Detalle_Compra_IdCompra')
    CREATE NONCLUSTERED INDEX IX_Detalle_Compra_IdCompra ON dbo.Detalle_Compra (IdCompra);

COMMIT TRANSACTION;
PRINT 'Índices y claves creados (o ya existían).';
