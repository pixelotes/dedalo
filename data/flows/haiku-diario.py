# GENERADO POR DÉDALO -- no editar a mano, se regenera desde el grafo.
# dedalo.VERSION = 1
# dedalo.graph_hash = 5ca1d930a255ee69
from dedalo import runtime
from dedalo.runtime import SKIPPED


def node_poeta(ctx, inputs):
    return runtime.agent(ctx, inputs, model='', per_item=False, prompt='Eres un poeta. Escribe un haiku breve y evocador sobre el amanecer o la naturaleza. Responde únicamente con el haiku.', secret='', tokens=2048, url='')


def node_guardar(ctx, inputs):
    return runtime.file(ctx, inputs, content='inputs', directory='', filename='haiku-{fecha}.md', message='', mode='sobrescribir')


def run(ctx):
    r_poeta = node_poeta(ctx, [])
    r_guardar = node_guardar(ctx, [r_poeta]) if r_poeta.ok else SKIPPED
    return runtime.summarize({'poeta': r_poeta, 'guardar': r_guardar}, ctx)
