"""
Pasa al hosting las fotos de productos que se subieron cuando se guardaban en la PC
(static/uploads/productos) y actualiza su dirección en la tabla Producto.

    python migrar_fotos_hosting.py            -> solo muestra qué haría (no cambia nada)
    python migrar_fotos_hosting.py --aplicar  -> sube las fotos y actualiza la base de datos

Si la foto de un producto no está en esta PC (la subió otro compañero), se indica:
ese compañero debe ejecutar este script en su PC, o volver a subir la foto desde
"Editar producto".
"""
import os
import sys

import db
import almacen_fotos

CARPETA_LOCAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'uploads', 'productos')
aplicar = '--aplicar' in sys.argv

if almacen_fotos.faltantes():
    print('Falta configurar en el .env:', ', '.join(almacen_fotos.faltantes()))
    sys.exit(1)

conexion = db.obtener_conexion()
cursor = conexion.cursor()
productos = cursor.execute(
    "SELECT IdProducto, Nombre, RutaImagen, NombreImagen FROM Producto "
    "WHERE RutaImagen LIKE '/static/uploads/productos/%' ORDER BY IdProducto").fetchall()
if not productos:
    print('No hay fotos guardadas en PC por migrar: todas están en el hosting.')
    sys.exit(0)

pendientes = 0
for p in productos:
    nombre = p.NombreImagen or p.RutaImagen.rsplit('/', 1)[-1]
    archivo_local = os.path.join(CARPETA_LOCAL, nombre)
    if not os.path.isfile(archivo_local):
        pendientes += 1
        print(f'  FALTA  {p.IdProducto} "{p.Nombre}": {nombre} no está en esta PC.')
        continue
    if not aplicar:
        print(f'  SUBIRÍA {p.IdProducto} "{p.Nombre}": {nombre}')
        continue
    with open(archivo_local, 'rb') as foto:
        url = almacen_fotos.subir(nombre, foto)
    # Solo se cambia si la fila sigue con la ruta antigua (nadie la cambió mientras tanto).
    cursor.execute("UPDATE Producto SET RutaImagen = ?, NombreImagen = ? WHERE IdProducto = ? AND RutaImagen = ?",
                   url, nombre, p.IdProducto, p.RutaImagen)
    conexion.commit()
    print(f'  OK     {p.IdProducto} "{p.Nombre}": {url}')

if not aplicar:
    print('\nNo se cambió nada. Para hacerlo: python migrar_fotos_hosting.py --aplicar')
elif pendientes:
    print(f'\n{pendientes} foto(s) no estaban en esta PC: súbelas de nuevo desde "Editar producto".')
conexion.close()
