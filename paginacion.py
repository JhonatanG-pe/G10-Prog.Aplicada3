"""
Paginación de listados.

SQL Server devuelve solo las filas de la página pedida (OFFSET ... FETCH NEXT),
no la tabla completa. Así el tiempo de respuesta y la memoria usada no crecen
con la cantidad de registros: ver 20 pedidos cuesta lo mismo si hay 50 o 50 000.

Uso en una ruta:
    pagina = consultar_pagina("SELECT ... FROM ... WHERE x = ?", valor_x, orden='Nombre, Id')
    return render_template('lista.html', filas=pagina)

En la plantilla, la página se recorre igual que una lista ({% for f in filas %})
y los controles se muestran con {{ paginacion(filas) }} (templates/_paginacion.html).
"""
from math import ceil
from flask import request, url_for
from db import consultar_todos, consultar_uno

POR_PAGINA = 20
PAGINA_MAXIMA = 1_000_000  # tope para un número de página escrito a mano en la URL


class Pagina:
    """Una página de resultados más los datos para dibujar los controles."""

    def __init__(self, items, numero, por_pagina, total):
        self.items = items
        self.numero = numero
        self.por_pagina = por_pagina
        self.total = total
        self.total_paginas = max(1, ceil(total / por_pagina))
        self.desde = (numero - 1) * por_pagina + 1 if total else 0
        self.hasta = min(numero * por_pagina, total)

    # Se comporta como la lista de filas de la página.
    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)

    def numeros(self, alrededor=2):
        """Números a mostrar: primera, última y las cercanas a la actual; None = '…'."""
        visibles = {1, self.total_paginas, *range(self.numero - alrededor, self.numero + alrededor + 1)}
        resultado, anterior = [], 0
        for n in sorted(v for v in visibles if 1 <= v <= self.total_paginas):
            if n - anterior > 1:
                resultado.append(None)
            resultado.append(n)
            anterior = n
        return resultado


def numero_de_pagina():
    """Lee ?pagina= de la URL. Cualquier valor inválido (texto, 0, negativo) se toma como 1."""
    valor = request.args.get('pagina', '1')
    if valor.isdigit() and 1 <= int(valor) <= PAGINA_MAXIMA:
        return int(valor)
    return 1


def consultar_pagina(sql, *parametros, orden, por_pagina=POR_PAGINA):
    """
    sql:   SELECT completo SIN ORDER BY (puede tener WHERE, JOIN y GROUP BY).
    orden: texto del ORDER BY. Lo escribe siempre el programador, nunca viene del
           usuario; debe terminar en una columna única (el Id) para que el orden
           sea estable y ninguna fila se repita o se pierda entre páginas.
    """
    total = consultar_uno(f"SELECT COUNT(*) FROM ({sql}) AS consulta", *parametros)[0]
    total_paginas = max(1, ceil(total / por_pagina))
    numero = min(numero_de_pagina(), total_paginas)  # una página que ya no existe muestra la última
    items = consultar_todos(
        f"{sql} ORDER BY {orden} OFFSET ? ROWS FETCH NEXT ? ROWS ONLY",
        *parametros, (numero - 1) * por_pagina, por_pagina,
    )
    return Pagina(items, numero, por_pagina, total)


def url_pagina(numero):
    """URL de la misma pantalla en otra página, conservando los filtros (ej. ?estado=Pendiente)."""
    argumentos = request.args.to_dict()
    argumentos['pagina'] = numero
    return url_for(request.endpoint, **(request.view_args or {}), **argumentos)
