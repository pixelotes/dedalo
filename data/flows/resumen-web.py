# GENERADO POR DÉDALO -- no editar a mano, se regenera desde el grafo.
# dedalo.VERSION = 1
# dedalo.graph_hash = 8d4733bcd956e475
from dedalo import runtime
from dedalo.runtime import SKIPPED


def node_pagina(ctx, inputs):
    return runtime.web_read(ctx, inputs, length='medium', url='https://example.com')


def node_resumir(ctx, inputs):
    return runtime.summarize_text(ctx, inputs, engine='luhn', length='short', prompt='')


def node_salida(ctx, inputs):
    return runtime.file(ctx, inputs, content='inputs', directory='', filename='resumen.txt', message='', mode='sobrescribir')


def run(ctx):
    r_pagina = node_pagina(ctx, [])
    r_resumir = node_resumir(ctx, [r_pagina]) if r_pagina.ok else SKIPPED
    r_salida = node_salida(ctx, [r_resumir]) if r_resumir.ok else SKIPPED
    return runtime.summarize({'pagina': r_pagina, 'resumir': r_resumir, 'salida': r_salida}, ctx)
