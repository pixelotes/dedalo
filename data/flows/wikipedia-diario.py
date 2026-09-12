# GENERADO POR DÉDALO -- no editar a mano, se regenera desde el grafo.
# dedalo.VERSION = 1
from dedalo import runtime
from dedalo.runtime import SKIPPED


def node_fetch_wikipedia(ctx, inputs):
    return runtime.http_call(ctx, inputs, auth_header='Authorization', body='', engine='auto', headers='', impersonate='chrome', method='GET', secret='', timeout=15.0, url='https://es.wikipedia.org/w/api.php?action=query&format=json&formatversion=2&prop=extracts|info&inprop=url&explaintext=1&generator=random&grnnamespace=0&grnlimit=1', user_agent='')


def node_resumir_luhn(ctx, inputs):
    return runtime.summarize_text(ctx, inputs, engine='luhn', field='query.pages.0.extract', length='long', prompt='', ratio=0.0, sentences=10)


def node_formatear_articulo(ctx, inputs):
    return runtime.template(ctx, inputs, template='# Artículo Diario de Wikipedia ({fecha})\n\n## {query.pages.0.title}\n- **Enlace:** {query.pages.0.canonicalurl}\n\n### Resumen Extenso del Artículo (Luhn):\n{entrada}\n')


def node_guardar_articulo(ctx, inputs):
    return runtime.file(ctx, inputs, content='inputs', directory='', filename='wikipedia-articulo-diario.md', format='raw', message='', mode='sobrescribir')


def run(ctx):
    r_fetch_wikipedia = node_fetch_wikipedia(ctx, [])
    r_resumir_luhn = node_resumir_luhn(ctx, [r_fetch_wikipedia]) if r_fetch_wikipedia.ok else SKIPPED
    r_formatear_articulo = node_formatear_articulo(ctx, [r_resumir_luhn, r_fetch_wikipedia]) if (r_resumir_luhn.ok and r_fetch_wikipedia.ok) else SKIPPED
    r_guardar_articulo = node_guardar_articulo(ctx, [r_formatear_articulo]) if r_formatear_articulo.ok else SKIPPED
    return runtime.summarize({'fetch_wikipedia': r_fetch_wikipedia, 'resumir_luhn': r_resumir_luhn, 'formatear_articulo': r_formatear_articulo, 'guardar_articulo': r_guardar_articulo}, ctx)
