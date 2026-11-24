/* =============================================================================
   Procedimientos almacenados - Sistema de carrito de compras
   Base de datos: db_a82a66_carrito (SQL Server 2016 o superior)

   Se ejecuta UNA vez en la base de datos (SSMS: abrir este archivo y F5),
   DESPUÉS de sql/facturacion_electronica.sql (crea las tablas de comprobantes).
   Usa CREATE OR ALTER: se puede volver a ejecutar para actualizar los
   procedimientos sin borrarlos antes. No modifica ni borra datos.

   Procedimientos (14):
     Transacciones (varios pasos que deben ocurrir todos o ninguno):
       1. usp_Venta_Registrar          HU-16  checkout: venta + detalle + stock + vaciar carrito
                                              + comprobante electrónico (boleta / factura)
       2. usp_Venta_CambiarEstado      HU-20  entregar / anular pedido (devuelve el stock)
       3. usp_Compra_Registrar         HU-11  compra + detalle + reposición de stock
     Reportes (exclusivos del Administrador):
       4. usp_Reporte_BajoStock        HU-12
       5. usp_Reporte_Valorizacion     HU-12
       6. usp_Reporte_HistorialCompras HU-12
       7. usp_Reporte_Ventas           HU-17
       8. usp_Reporte_MasVendidos      HU-17
     Seguridad (la contraseña se compara dentro de SQL Server, HU-05):
       9. usp_Usuario_Login            HU-01
      10. usp_Cliente_Login            HU-13
      11. usp_Cliente_CambiarClave     HU-21
     Facturación electrónica SUNAT:
      12. usp_Comprobante_Obtener            datos del comprobante (cabecera + emisor, y detalle)
      13. usp_Comprobante_RegistrarRespuesta guarda XML firmado, CDR y estado devuelto por SUNAT
      14. usp_Comprobante_ObtenerArchivo     XML firmado o CDR para descargar

   Manejo de transacciones de los procedimientos 1 a 3:
     Python (pyodbc) ya trabaja dentro de una transacción. Por eso, si al entrar
     ya hay una transacción abierta, el procedimiento marca un punto de guardado
     (SAVE TRANSACTION) y, si algo falla, deshace solo su propio trabajo; si no
     hay ninguna, abre y confirma la suya. Así funcionan igual llamados desde la
     aplicación o directamente desde SSMS.

   Resultado de los procedimientos 1 a 3: una fila con la columna Resultado
     0 = correcto; otro número = el motivo por el que no se hizo nada (ver cada
     procedimiento). Un error inesperado deshace todo y se lanza (THROW).
   ============================================================================= */


/* -----------------------------------------------------------------------------
   1. usp_Venta_Registrar (HU-16 + facturación electrónica)
   Registra el pedido con el contenido ACTUAL del carrito del cliente:
   Venta (Pendiente) + Detalle_Venta + descuento de stock + carrito vacío
   + Comprobante (Boleta o Factura) con su serie, correlativo, valor de venta e IGV.
   Requiere las tablas de sql/facturacion_electronica.sql.
   Resultado: 0 = correcto (IdVenta, IdComprobante, NumeroComprobante)
              1 = carrito vacío
              2 = producto inactivo (Producto)
              3 = stock insuficiente (Producto, Disponible)
              4 = el total supera el máximo de la columna MontoTotal
              5 = el carrito cambió durante la confirmación
              6 = datos del comprobante no válidos (Factura: RUC de 11 dígitos y razón
                  social; Boleta: DNI de 8 dígitos, obligatorio si el total supera S/ 700)
              7 = no hay una serie activa para ese tipo de comprobante
   ----------------------------------------------------------------------------- */
CREATE OR ALTER PROCEDURE dbo.usp_Venta_Registrar
    @IdCliente        INT,
    @Contacto         VARCHAR(100),
    @Telefono         VARCHAR(20)  = NULL,
    @Direccion        VARCHAR(300),
    @FechaVenta       DATETIME,            -- la envía la aplicación (hora de Perú, no la del servidor)
    @TipoComprobante  CHAR(2)      = '03', -- 03 = Boleta, 01 = Factura
    @ClienteTipoDoc   CHAR(1)      = '0',  -- 6 = RUC, 1 = DNI, 0 = sin documento (solo boleta <= S/ 700)
    @ClienteNumDoc    VARCHAR(15)  = '-',
    @ClienteNombre    VARCHAR(200) = NULL, -- razón social (factura); en boleta, si falta, el nombre del cliente
    @ClienteDireccion VARCHAR(300) = NULL  -- dirección fiscal (opcional)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT OFF;   -- permite deshacer solo hasta el punto de guardado

    DECLARE @TranLocal BIT = 0, @Resultado INT = 0, @IdVenta INT = NULL,
            @Producto VARCHAR(500) = NULL, @Disponible INT = NULL,
            @Lineas INT, @TotalProducto INT, @MontoTotal DECIMAL(19, 2),
            @IdComprobante INT = NULL, @Serie CHAR(4) = NULL, @Correlativo INT = NULL;
    DECLARE @Items TABLE (IdProducto INT PRIMARY KEY, Nombre VARCHAR(500), Cantidad INT,
                          Precio DECIMAL(10, 2), Stock INT, Activo BIT);
    DECLARE @LineasComprobante TABLE (Item INT, IdProducto INT, Descripcion VARCHAR(500), Cantidad INT,
                                      ValorUnitario DECIMAL(20, 10), PrecioUnitario DECIMAL(10, 2),
                                      ValorVenta DECIMAL(12, 2), IGV DECIMAL(12, 2));

    SET @ClienteNumDoc = LTRIM(RTRIM(@ClienteNumDoc));
    SET @ClienteNombre = NULLIF(LTRIM(RTRIM(@ClienteNombre)), '');
    IF @TipoComprobante = '03' AND @ClienteNombre IS NULL
        SELECT @ClienteNombre = LEFT(Nombres + ' ' + Apellidos, 200) FROM dbo.Cliente WHERE IdCliente = @IdCliente;

    IF @@TRANCOUNT = 0 BEGIN BEGIN TRANSACTION; SET @TranLocal = 1; END
    ELSE SAVE TRANSACTION usp_Venta_Registrar;

    BEGIN TRY
        -- Bloquea el carrito del cliente y sus productos hasta terminar: un doble clic
        -- en "Confirmar" espera aquí y luego encuentra el carrito ya vacío.
        INSERT INTO @Items (IdProducto, Nombre, Cantidad, Precio, Stock, Activo)
        SELECT p.IdProducto, p.Nombre, ca.Cantidad, p.Precio, p.Stock, p.Activo
        FROM dbo.Carrito ca WITH (UPDLOCK, HOLDLOCK)
        INNER JOIN dbo.Producto p WITH (UPDLOCK) ON p.IdProducto = ca.IdProducto
        WHERE ca.IdCliente = @IdCliente;

        SELECT @Lineas = COUNT(*), @TotalProducto = SUM(Cantidad), @MontoTotal = SUM(Cantidad * Precio)
        FROM @Items;

        IF @Lineas = 0
        BEGIN SET @Resultado = 1; GOTO Fin; END

        SELECT TOP (1) @Producto = Nombre FROM @Items WHERE Activo = 0 ORDER BY Nombre;
        IF @Producto IS NOT NULL
        BEGIN SET @Resultado = 2; GOTO Fin; END

        SELECT TOP (1) @Producto = Nombre, @Disponible = Stock FROM @Items WHERE Cantidad > Stock ORDER BY Nombre;
        IF @Producto IS NOT NULL
        BEGIN SET @Resultado = 3; GOTO Fin; END

        IF @MontoTotal > 99999999.99
        BEGIN SET @Resultado = 4; GOTO Fin; END

        -- Datos del comprobante (catálogos 01 y 06 de SUNAT).
        IF NOT (   (@TipoComprobante = '01' AND @ClienteTipoDoc = '6'
                    AND @ClienteNumDoc LIKE REPLICATE('[0-9]', 11) AND @ClienteNombre IS NOT NULL)
                OR (@TipoComprobante = '03' AND @ClienteTipoDoc = '1'
                    AND @ClienteNumDoc LIKE REPLICATE('[0-9]', 8) AND @ClienteNombre IS NOT NULL)
                OR (@TipoComprobante = '03' AND @ClienteTipoDoc = '0'
                    AND @ClienteNumDoc = '-' AND @ClienteNombre IS NOT NULL AND @MontoTotal <= 700))
        BEGIN SET @Resultado = 6; GOTO Fin; END

        INSERT INTO dbo.Venta (IdCliente, TotalProducto, MontoTotal, Contacto, Telefono, Direccion, FechaVenta, Estado)
        VALUES (@IdCliente, @TotalProducto, @MontoTotal, @Contacto, @Telefono, @Direccion, @FechaVenta, 'Pendiente');
        SET @IdVenta = SCOPE_IDENTITY();

        INSERT INTO dbo.Detalle_Venta (IdVenta, IdProducto, Cantidad, Precio)
        SELECT @IdVenta, IdProducto, Cantidad, Precio FROM @Items;

        -- Verifica y descuenta en un solo paso: nunca deja un stock negativo.
        UPDATE p
        SET p.Stock = p.Stock - i.Cantidad
        FROM dbo.Producto p
        INNER JOIN @Items i ON i.IdProducto = p.IdProducto
        WHERE p.Activo = 1 AND p.Stock >= i.Cantidad;
        IF @@ROWCOUNT <> @Lineas
        BEGIN SET @Resultado = 3; SET @IdVenta = NULL; GOTO Fin; END

        DELETE FROM dbo.Carrito WHERE IdCliente = @IdCliente;
        IF @@ROWCOUNT <> @Lineas
        BEGIN SET @Resultado = 5; SET @IdVenta = NULL; GOTO Fin; END

        -- Comprobante electrónico. El siguiente correlativo se toma y se reserva en un solo
        -- UPDATE: la fila de la serie queda bloqueada hasta confirmar, así dos ventas
        -- simultáneas nunca reciben el mismo número ni quedan huecos.
        UPDATE dbo.SerieComprobante
        SET @Correlativo = UltimoCorrelativo = UltimoCorrelativo + 1, @Serie = Serie
        WHERE IdSerie = (SELECT TOP (1) IdSerie FROM dbo.SerieComprobante
                         WHERE TipoComprobante = @TipoComprobante AND Activo = 1 ORDER BY Serie);
        IF @Correlativo IS NULL
        BEGIN SET @Resultado = 7; SET @IdVenta = NULL; GOTO Fin; END

        -- Los precios del catálogo incluyen IGV: por línea, valor de venta = total / 1.18
        -- (redondeado al céntimo) e IGV = total - valor de venta, así la suma cuadra
        -- exactamente con el monto cobrado.
        INSERT INTO @LineasComprobante (Item, IdProducto, Descripcion, Cantidad, ValorUnitario,
                                        PrecioUnitario, ValorVenta, IGV)
        SELECT ROW_NUMBER() OVER (ORDER BY Nombre, IdProducto), IdProducto, Nombre, Cantidad,
               ROUND(CAST(Precio AS DECIMAL(20, 10)) / 1.18, 10),
               Precio,
               ROUND(CAST(Cantidad * Precio AS DECIMAL(20, 10)) / 1.18, 2),
               Cantidad * Precio - ROUND(CAST(Cantidad * Precio AS DECIMAL(20, 10)) / 1.18, 2)
        FROM @Items;

        INSERT INTO dbo.Comprobante (IdVenta, TipoComprobante, Serie, Correlativo, FechaEmision,
                                     ClienteTipoDoc, ClienteNumDoc, ClienteNombre, ClienteDireccion,
                                     OpGravadas, IGV, Total)
        SELECT @IdVenta, @TipoComprobante, @Serie, @Correlativo, @FechaVenta,
               @ClienteTipoDoc, @ClienteNumDoc, @ClienteNombre, NULLIF(LTRIM(RTRIM(@ClienteDireccion)), ''),
               SUM(ValorVenta), SUM(IGV), SUM(ValorVenta + IGV)
        FROM @LineasComprobante;
        SET @IdComprobante = SCOPE_IDENTITY();

        INSERT INTO dbo.Comprobante_Detalle (IdComprobante, Item, IdProducto, Descripcion, Cantidad,
                                             ValorUnitario, PrecioUnitario, ValorVenta, IGV)
        SELECT @IdComprobante, Item, IdProducto, Descripcion, Cantidad, ValorUnitario, PrecioUnitario, ValorVenta, IGV
        FROM @LineasComprobante;

    Fin:
        IF @Resultado = 0
        BEGIN
            IF @TranLocal = 1 COMMIT TRANSACTION;
        END
        ELSE
        BEGIN
            IF @TranLocal = 1 ROLLBACK TRANSACTION;
            ELSE ROLLBACK TRANSACTION usp_Venta_Registrar;
            SELECT @IdComprobante = NULL, @Serie = NULL, @Correlativo = NULL;
        END

        SELECT @Resultado AS Resultado, @IdVenta AS IdVenta, @Producto AS Producto, @Disponible AS Disponible,
               @IdComprobante AS IdComprobante, @Serie + '-' + CAST(@Correlativo AS VARCHAR(10)) AS NumeroComprobante;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0
        BEGIN
            IF @TranLocal = 1 OR XACT_STATE() = -1 ROLLBACK TRANSACTION;
            ELSE ROLLBACK TRANSACTION usp_Venta_Registrar;
        END;
        THROW;
    END CATCH
END
GO


/* -----------------------------------------------------------------------------
   2. usp_Venta_CambiarEstado (HU-20)
   Solo un pedido Pendiente cambia de estado. Al anularlo, devuelve el stock de
   todos sus productos en la misma transacción.
   Resultado: 0 = correcto   1 = el pedido no existe o ya no está Pendiente
              2 = estado nuevo no válido
   ----------------------------------------------------------------------------- */
CREATE OR ALTER PROCEDURE dbo.usp_Venta_CambiarEstado
    @IdVenta     INT,
    @NuevoEstado VARCHAR(20)          -- 'Entregado' o 'Anulado'
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT OFF;

    DECLARE @TranLocal BIT = 0, @Resultado INT = 0, @Productos INT;

    IF @NuevoEstado NOT IN ('Entregado', 'Anulado')
    BEGIN
        SELECT 2 AS Resultado;
        RETURN;
    END

    IF @@TRANCOUNT = 0 BEGIN BEGIN TRANSACTION; SET @TranLocal = 1; END
    ELSE SAVE TRANSACTION usp_Venta_CambiarEstado;

    BEGIN TRY
        -- La condición va en el mismo UPDATE: si dos usuarios actúan a la vez, solo uno lo consigue.
        UPDATE dbo.Venta SET Estado = @NuevoEstado
        WHERE IdVenta = @IdVenta AND Estado = 'Pendiente';

        IF @@ROWCOUNT <> 1
            SET @Resultado = 1;
        ELSE IF @NuevoEstado = 'Anulado'
        BEGIN
            SELECT @Productos = COUNT(DISTINCT IdProducto) FROM dbo.Detalle_Venta WHERE IdVenta = @IdVenta;

            UPDATE p
            SET p.Stock = p.Stock + d.Cantidad
            FROM dbo.Producto p
            INNER JOIN (SELECT IdProducto, SUM(Cantidad) AS Cantidad
                        FROM dbo.Detalle_Venta
                        WHERE IdVenta = @IdVenta
                        GROUP BY IdProducto) d ON d.IdProducto = p.IdProducto;

            IF @@ROWCOUNT <> @Productos
                THROW 50001, 'No se pudo devolver el stock de todos los productos del pedido.', 1;
        END

        IF @Resultado = 0
        BEGIN
            IF @TranLocal = 1 COMMIT TRANSACTION;
        END
        ELSE
        BEGIN
            IF @TranLocal = 1 ROLLBACK TRANSACTION;
            ELSE ROLLBACK TRANSACTION usp_Venta_CambiarEstado;
        END

        SELECT @Resultado AS Resultado;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0
        BEGIN
            IF @TranLocal = 1 OR XACT_STATE() = -1 ROLLBACK TRANSACTION;
            ELSE ROLLBACK TRANSACTION usp_Venta_CambiarEstado;
        END;
        THROW;
    END CATCH
END
GO


/* -----------------------------------------------------------------------------
   3. usp_Compra_Registrar (HU-11)
   Registra la compra y repone el stock. Las líneas llegan en JSON:
     [{"IdProducto": 5, "Cantidad": 10, "Precio": "12.50"}, ...]
   SubTotal = suma de (cantidad x precio); IGV = 18% redondeado al céntimo;
   Total = SubTotal + IGV.
   Resultado: 0 = correcto (IdCompra)   1 = sin líneas de detalle
              2 = un producto no existe o está inactivo (IdProducto)
              3 = el total supera el máximo de las columnas
   ----------------------------------------------------------------------------- */
CREATE OR ALTER PROCEDURE dbo.usp_Compra_Registrar
    @IdProveedor INT,
    @IdUsuario   INT,
    @Fecha       DATETIME,            -- la envía la aplicación (hora de Perú)
    @Detalle     NVARCHAR(MAX)        -- líneas de la compra en JSON
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT OFF;

    DECLARE @TranLocal BIT = 0, @Resultado INT = 0, @IdCompra INT = NULL, @IdProducto INT = NULL,
            @SubTotal DECIMAL(19, 2), @IGV DECIMAL(19, 2), @Productos INT;
    DECLARE @Lineas TABLE (Linea INT IDENTITY(1, 1), IdProducto INT, Cantidad INT, Precio DECIMAL(10, 2));

    INSERT INTO @Lineas (IdProducto, Cantidad, Precio)
    SELECT IdProducto, Cantidad, Precio
    FROM OPENJSON(@Detalle)
         WITH (IdProducto INT '$.IdProducto', Cantidad INT '$.Cantidad', Precio DECIMAL(10, 2) '$.Precio');

    IF NOT EXISTS (SELECT 1 FROM @Lineas)
    BEGIN
        SELECT 1 AS Resultado, NULL AS IdCompra, NULL AS IdProducto;
        RETURN;
    END

    SELECT @SubTotal = SUM(CAST(Cantidad AS DECIMAL(19, 2)) * Precio) FROM @Lineas;
    SET @IGV = ROUND(@SubTotal * 0.18, 2);   -- ROUND redondea la mitad hacia arriba
    IF @SubTotal + @IGV > 99999999.99
    BEGIN
        SELECT 3 AS Resultado, NULL AS IdCompra, NULL AS IdProducto;
        RETURN;
    END

    IF @@TRANCOUNT = 0 BEGIN BEGIN TRANSACTION; SET @TranLocal = 1; END
    ELSE SAVE TRANSACTION usp_Compra_Registrar;

    BEGIN TRY
        SELECT TOP (1) @IdProducto = l.IdProducto
        FROM @Lineas l
        LEFT JOIN dbo.Producto p WITH (UPDLOCK) ON p.IdProducto = l.IdProducto AND p.Activo = 1
        WHERE p.IdProducto IS NULL
        ORDER BY l.Linea;
        IF @IdProducto IS NOT NULL
        BEGIN SET @Resultado = 2; GOTO Fin; END

        INSERT INTO dbo.Compra (Fecha, IdProveedor, SubTotal, IGV, Total, IdUsuario)
        VALUES (@Fecha, @IdProveedor, @SubTotal, @IGV, @SubTotal + @IGV, @IdUsuario);
        SET @IdCompra = SCOPE_IDENTITY();

        INSERT INTO dbo.Detalle_Compra (IdCompra, IdProducto, Cantidad, Precio)
        SELECT @IdCompra, IdProducto, Cantidad, Precio FROM @Lineas ORDER BY Linea;

        -- Reposición automática de stock (un mismo producto puede venir en varias líneas).
        SELECT @Productos = COUNT(DISTINCT IdProducto) FROM @Lineas;
        UPDATE p
        SET p.Stock = p.Stock + l.Cantidad
        FROM dbo.Producto p
        INNER JOIN (SELECT IdProducto, SUM(Cantidad) AS Cantidad FROM @Lineas GROUP BY IdProducto) l
                ON l.IdProducto = p.IdProducto
        WHERE p.Activo = 1;
        IF @@ROWCOUNT <> @Productos
            THROW 50002, 'No se pudo reponer el stock de todos los productos de la compra.', 1;

    Fin:
        IF @Resultado = 0
        BEGIN
            IF @TranLocal = 1 COMMIT TRANSACTION;
        END
        ELSE
        BEGIN
            IF @TranLocal = 1 ROLLBACK TRANSACTION;
            ELSE ROLLBACK TRANSACTION usp_Compra_Registrar;
        END

        SELECT @Resultado AS Resultado, @IdCompra AS IdCompra, @IdProducto AS IdProducto;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0
        BEGIN
            IF @TranLocal = 1 OR XACT_STATE() = -1 ROLLBACK TRANSACTION;
            ELSE ROLLBACK TRANSACTION usp_Compra_Registrar;
        END;
        THROW;
    END CATCH
END
GO


/* -----------------------------------------------------------------------------
   4. usp_Reporte_BajoStock (HU-12)
   Productos activos con Stock en o por debajo de su mínimo definido.
   ----------------------------------------------------------------------------- */
CREATE OR ALTER PROCEDURE dbo.usp_Reporte_BajoStock
AS
BEGIN
    SET NOCOUNT ON;

    SELECT p.IdProducto, p.Nombre, c.Descripcion AS Categoria, m.Descripcion AS Marca,
           p.Stock, p.StockMinimo
    FROM dbo.Producto p
    LEFT JOIN dbo.Categoria c ON c.IdCategoria = p.IdCategoria
    LEFT JOIN dbo.Marca m ON m.IdMarca = p.IdMarca
    WHERE p.Activo = 1 AND p.Stock <= p.StockMinimo
    ORDER BY p.Stock ASC, p.Nombre;
END
GO


/* -----------------------------------------------------------------------------
   5. usp_Reporte_Valorizacion (HU-12)
   Valor por producto activo (Stock x Precio). El total general lo suma la aplicación.
   ----------------------------------------------------------------------------- */
CREATE OR ALTER PROCEDURE dbo.usp_Reporte_Valorizacion
AS
BEGIN
    SET NOCOUNT ON;

    SELECT p.IdProducto, p.Nombre, c.Descripcion AS Categoria, p.Stock, p.Precio,
           (p.Stock * p.Precio) AS ValorTotal
    FROM dbo.Producto p
    LEFT JOIN dbo.Categoria c ON c.IdCategoria = p.IdCategoria
    WHERE p.Activo = 1
    ORDER BY ValorTotal DESC, p.Nombre;
END
GO


/* -----------------------------------------------------------------------------
   6. usp_Reporte_HistorialCompras (HU-12)
   Filtros opcionales (NULL = sin filtro). @Hasta incluye todo ese día.
   OPTION (RECOMPILE): con filtros opcionales, SQL Server arma el mejor plan
   para los filtros realmente usados y aprovecha los índices.
   ----------------------------------------------------------------------------- */
CREATE OR ALTER PROCEDURE dbo.usp_Reporte_HistorialCompras
    @IdProveedor INT  = NULL,
    @Desde       DATE = NULL,
    @Hasta       DATE = NULL
AS
BEGIN
    SET NOCOUNT ON;

    SELECT c.IdCompra, c.Fecha, pr.RazonSocial, c.Total, u.Nombres, u.ApellidoPaterno
    FROM dbo.Compra c
    INNER JOIN dbo.Proveedor pr ON pr.IdProveedor = c.IdProveedor
    LEFT JOIN dbo.Usuario u ON u.IdUsuario = c.IdUsuario
    WHERE (@IdProveedor IS NULL OR c.IdProveedor = @IdProveedor)
      AND (@Desde IS NULL OR c.Fecha >= @Desde)
      AND (@Hasta IS NULL OR c.Fecha < DATEADD(DAY, 1, CAST(@Hasta AS DATETIME)))
    ORDER BY c.Fecha DESC
    OPTION (RECOMPILE);
END
GO


/* -----------------------------------------------------------------------------
   7. usp_Reporte_Ventas (HU-17)
   Ventas filtrables por cliente y/o rango de fechas. Los pedidos Anulados no cuentan.
   ----------------------------------------------------------------------------- */
CREATE OR ALTER PROCEDURE dbo.usp_Reporte_Ventas
    @IdCliente INT  = NULL,
    @Desde     DATE = NULL,
    @Hasta     DATE = NULL
AS
BEGIN
    SET NOCOUNT ON;

    SELECT v.IdVenta, v.FechaVenta, cl.Nombres, cl.Apellidos, v.TotalProducto, v.MontoTotal, v.Estado
    FROM dbo.Venta v
    INNER JOIN dbo.Cliente cl ON cl.IdCliente = v.IdCliente
    WHERE v.Estado <> 'Anulado'
      AND (@IdCliente IS NULL OR v.IdCliente = @IdCliente)
      AND (@Desde IS NULL OR v.FechaVenta >= @Desde)
      AND (@Hasta IS NULL OR v.FechaVenta < DATEADD(DAY, 1, CAST(@Hasta AS DATETIME)))
    ORDER BY v.FechaVenta DESC
    OPTION (RECOMPILE);
END
GO


/* -----------------------------------------------------------------------------
   8. usp_Reporte_MasVendidos (HU-17)
   Ranking por unidades vendidas, sin contar los pedidos Anulados.
   ----------------------------------------------------------------------------- */
CREATE OR ALTER PROCEDURE dbo.usp_Reporte_MasVendidos
AS
BEGIN
    SET NOCOUNT ON;

    SELECT p.IdProducto, p.Nombre, c.Descripcion AS Categoria, m.Descripcion AS Marca,
           SUM(dv.Cantidad) AS Unidades,
           SUM(dv.Cantidad * dv.Precio) AS Importe,
           COUNT(DISTINCT dv.IdVenta) AS Ventas
    FROM dbo.Detalle_Venta dv
    INNER JOIN dbo.Venta v ON v.IdVenta = dv.IdVenta
    INNER JOIN dbo.Producto p ON p.IdProducto = dv.IdProducto
    LEFT JOIN dbo.Categoria c ON c.IdCategoria = p.IdCategoria
    LEFT JOIN dbo.Marca m ON m.IdMarca = p.IdMarca
    WHERE v.Estado <> 'Anulado'
    GROUP BY p.IdProducto, p.Nombre, c.Descripcion, m.Descripcion
    ORDER BY Unidades DESC, Importe DESC, p.Nombre;
END
GO


/* -----------------------------------------------------------------------------
   9. usp_Usuario_Login (HU-01, HU-05)
   Devuelve el usuario solo si el correo existe Y la contraseña coincide. El hash
   se recalcula aquí con la Salt guardada: el hash real nunca sale de SQL Server.
   @Clave es VARCHAR a propósito: HASHBYTES de un NVARCHAR da otro resultado.
   ----------------------------------------------------------------------------- */
CREATE OR ALTER PROCEDURE dbo.usp_Usuario_Login
    @Correo VARCHAR(255),
    @Clave  VARCHAR(255)
AS
BEGIN
    SET NOCOUNT ON;

    SELECT IdUsuario, Nombres, ApellidoPaterno, EstadoRegistro
    FROM dbo.Usuario
    WHERE CorreoElectronico = @Correo
      AND Clave = HASHBYTES('SHA2_256', @Clave + Salt);
END
GO


/* -----------------------------------------------------------------------------
   10. usp_Cliente_Login (HU-13, HU-05)
   ----------------------------------------------------------------------------- */
CREATE OR ALTER PROCEDURE dbo.usp_Cliente_Login
    @Correo VARCHAR(255),
    @Clave  VARCHAR(255)
AS
BEGIN
    SET NOCOUNT ON;

    SELECT IdCliente, Nombres, Activo
    FROM dbo.Cliente
    WHERE Correo = @Correo
      AND Clave = HASHBYTES('SHA2_256', @Clave + Salt);
END
GO


/* -----------------------------------------------------------------------------
   11. usp_Cliente_CambiarClave (HU-21, HU-05)
   Cambia la contraseña solo si la actual es correcta, con una Salt NUEVA, en una
   sola instrucción. Resultado: 1 = cambiada, 0 = contraseña actual incorrecta.
   ----------------------------------------------------------------------------- */
CREATE OR ALTER PROCEDURE dbo.usp_Cliente_CambiarClave
    @IdCliente   INT,
    @ClaveActual VARCHAR(255),
    @ClaveNueva  VARCHAR(255),
    @SaltNueva   VARCHAR(32)
AS
BEGIN
    SET NOCOUNT ON;

    UPDATE dbo.Cliente
    SET Clave = HASHBYTES('SHA2_256', @ClaveNueva + @SaltNueva),
        Salt  = @SaltNueva
    WHERE IdCliente = @IdCliente
      AND Clave = HASHBYTES('SHA2_256', @ClaveActual + Salt);

    SELECT @@ROWCOUNT AS Resultado;
END
GO


/* =============================================================================
   FACTURACIÓN ELECTRÓNICA SUNAT
   ============================================================================= */

/* -----------------------------------------------------------------------------
   12. usp_Comprobante_Obtener
   Devuelve DOS conjuntos de resultados:
     1) cabecera del comprobante + datos del emisor (Empresa) + estado del pedido
     2) líneas del comprobante
   Se busca por @IdComprobante o por @IdVenta. Si se envía @IdCliente, solo se
   devuelve cuando el pedido es de ese cliente (la tienda no muestra comprobantes ajenos).
   ----------------------------------------------------------------------------- */
CREATE OR ALTER PROCEDURE dbo.usp_Comprobante_Obtener
    @IdComprobante INT = NULL,
    @IdVenta       INT = NULL,
    @IdCliente     INT = NULL
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @Id INT;
    SELECT @Id = c.IdComprobante
    FROM dbo.Comprobante c
    INNER JOIN dbo.Venta v ON v.IdVenta = c.IdVenta
    WHERE (c.IdComprobante = @IdComprobante OR (@IdComprobante IS NULL AND c.IdVenta = @IdVenta))
      AND (@IdCliente IS NULL OR v.IdCliente = @IdCliente);

    SELECT c.IdComprobante, c.IdVenta, c.TipoComprobante, c.Serie, c.Correlativo,
           c.Serie + '-' + CAST(c.Correlativo AS VARCHAR(10)) AS NumeroComprobante,
           c.FechaEmision, c.Moneda, c.ClienteTipoDoc, c.ClienteNumDoc, c.ClienteNombre,
           c.ClienteDireccion, c.OpGravadas, c.IGV, c.Total, c.EstadoSunat, c.CodigoRespuesta,
           c.DescripcionRespuesta, c.Observaciones, c.HashCpe, c.FechaEnvio, c.Intentos,
           CAST(CASE WHEN c.XmlFirmado IS NULL THEN 0 ELSE 1 END AS BIT) AS TieneXml,
           CAST(CASE WHEN c.CdrZip IS NULL THEN 0 ELSE 1 END AS BIT) AS TieneCdr,
           v.IdCliente, v.Estado AS EstadoPedido,
           e.Ruc AS EmisorRuc, e.RazonSocial AS EmisorRazonSocial,
           e.NombreComercial AS EmisorNombreComercial, e.Direccion AS EmisorDireccion,
           e.Ubigeo AS EmisorUbigeo, e.Departamento AS EmisorDepartamento,
           e.Provincia AS EmisorProvincia, e.Distrito AS EmisorDistrito,
           e.CodigoEstablecimiento AS EmisorCodigoEstablecimiento
    FROM dbo.Comprobante c
    INNER JOIN dbo.Venta v ON v.IdVenta = c.IdVenta
    CROSS JOIN dbo.Empresa e
    WHERE c.IdComprobante = @Id AND e.IdEmpresa = 1;

    SELECT d.Item, d.IdProducto, d.Descripcion, d.Cantidad, d.UnidadMedida,
           d.ValorUnitario, d.PrecioUnitario, d.ValorVenta, d.IGV,
           d.ValorVenta + d.IGV AS Importe
    FROM dbo.Comprobante_Detalle d
    WHERE d.IdComprobante = @Id
    ORDER BY d.Item;
END
GO

/* -----------------------------------------------------------------------------
   13. usp_Comprobante_RegistrarRespuesta
   Guarda el resultado de un envío a SUNAT: estado, código y descripción de la
   respuesta, observaciones, hash de la firma, XML firmado y CDR. Suma un intento.
   Solo actualiza comprobantes PENDIENTE o ERROR: un comprobante ya ACEPTADO,
   OBSERVADO o RECHAZADO por SUNAT es definitivo y no se sobrescribe.
   Resultado: 1 = registrado, 0 = no existe o ya tenía respuesta definitiva.
   ----------------------------------------------------------------------------- */
CREATE OR ALTER PROCEDURE dbo.usp_Comprobante_RegistrarRespuesta
    @IdComprobante        INT,
    @EstadoSunat          VARCHAR(15),
    @CodigoRespuesta      VARCHAR(10)    = NULL,
    @DescripcionRespuesta VARCHAR(1000)  = NULL,
    @Observaciones        VARCHAR(2000)  = NULL,
    @HashCpe              VARCHAR(100)   = NULL,
    @XmlFirmado           VARBINARY(MAX) = NULL,
    @CdrZip               VARBINARY(MAX) = NULL,
    @FechaEnvio           DATETIME
AS
BEGIN
    SET NOCOUNT ON;

    UPDATE dbo.Comprobante
    SET EstadoSunat          = @EstadoSunat,
        CodigoRespuesta      = @CodigoRespuesta,
        DescripcionRespuesta = LEFT(@DescripcionRespuesta, 1000),
        Observaciones        = LEFT(@Observaciones, 2000),
        HashCpe              = COALESCE(@HashCpe, HashCpe),
        XmlFirmado           = COALESCE(@XmlFirmado, XmlFirmado),
        CdrZip               = COALESCE(@CdrZip, CdrZip),
        FechaEnvio           = @FechaEnvio,
        Intentos             = Intentos + 1
    WHERE IdComprobante = @IdComprobante
      AND EstadoSunat IN ('PENDIENTE', 'ERROR');

    SELECT @@ROWCOUNT AS Resultado;
END
GO

/* -----------------------------------------------------------------------------
   14. usp_Comprobante_ObtenerArchivo
   Devuelve el XML firmado (@Tipo = 'XML') o el CDR de SUNAT (@Tipo = 'CDR') del
   comprobante de un pedido, con el nombre de archivo oficial
   (RUC-TIPO-SERIE-NUMERO). Con @IdCliente, solo si el pedido es de ese cliente.
   ----------------------------------------------------------------------------- */
CREATE OR ALTER PROCEDURE dbo.usp_Comprobante_ObtenerArchivo
    @IdVenta   INT,
    @Tipo      VARCHAR(3),
    @IdCliente INT = NULL
AS
BEGIN
    SET NOCOUNT ON;

    SELECT CASE WHEN @Tipo = 'CDR' THEN 'R-' ELSE '' END
             + e.Ruc + '-' + c.TipoComprobante + '-' + c.Serie + '-' + CAST(c.Correlativo AS VARCHAR(10))
             + CASE WHEN @Tipo = 'CDR' THEN '.zip' ELSE '.xml' END AS NombreArchivo,
           CASE WHEN @Tipo = 'CDR' THEN c.CdrZip ELSE c.XmlFirmado END AS Contenido
    FROM dbo.Comprobante c
    INNER JOIN dbo.Venta v ON v.IdVenta = c.IdVenta
    CROSS JOIN dbo.Empresa e
    WHERE c.IdVenta = @IdVenta
      AND e.IdEmpresa = 1
      AND @Tipo IN ('XML', 'CDR')
      AND (@IdCliente IS NULL OR v.IdCliente = @IdCliente)
      AND CASE WHEN @Tipo = 'CDR' THEN c.CdrZip ELSE c.XmlFirmado END IS NOT NULL;
END
GO
