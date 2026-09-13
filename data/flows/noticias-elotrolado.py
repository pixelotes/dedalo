# GENERADO POR DÉDALO -- no editar a mano, se regenera desde el grafo.
# dedalo.VERSION = 1
# dedalo.graph_hash = 2498e6180ce7de77
from dedalo import runtime
from dedalo.runtime import SKIPPED


def node_descargar_eol(ctx, inputs):
    return runtime.http_call(ctx, inputs, auth_header='Authorization', body='', engine='auto', headers='', impersonate='chrome', method='GET', secret='', timeout=20.0, url='https://www.elotrolado.net/', user_agent='')


def node_extraer_noticias(ctx, inputs):
    return runtime.html_extract(ctx, inputs, base_url='https://www.elotrolado.net', fields='{"category": "a.newcat", "title": "h2 a", "link": "h2 a@href", "summary": "div.body"}', header_text='# Noticias de Portada — ElOtroLado.net ({fecha})\n\n', item_template='### [{category}] {title}\n- **Enlace:** {link}\n- **Resumen:** {summary}\n', limit=10, output_format='text', selector='div.new')


def node_guardar_noticias(ctx, inputs):
    return runtime.file(ctx, inputs, content='inputs', directory='', filename='elotrolado-noticias.md', format='raw', message='', mode='sobrescribir')


def run(ctx):
    r_descargar_eol = node_descargar_eol(ctx, [])
    r_extraer_noticias = node_extraer_noticias(ctx, [r_descargar_eol]) if r_descargar_eol.ok else SKIPPED
    r_guardar_noticias = node_guardar_noticias(ctx, [r_extraer_noticias]) if r_extraer_noticias.ok else SKIPPED
    return runtime.summarize({'descargar_eol': r_descargar_eol, 'extraer_noticias': r_extraer_noticias, 'guardar_noticias': r_guardar_noticias}, ctx)
