# GENERADO POR DÉDALO -- no editar a mano, se regenera desde el grafo.
# dedalo.VERSION = 1
from dedalo import runtime
from dedalo.runtime import SKIPPED


def node_descargar_mathom(ctx, inputs):
    return runtime.http_call(ctx, inputs, auth_header='Authorization', body='', engine='auto', headers='', impersonate='chrome', method='GET', secret='', timeout=15.0, url='https://mathom.es/es/nuevos-productos', user_agent='')


def node_extraer_mathom(ctx, inputs):
    return runtime.html_extract(ctx, inputs, base_url='https://mathom.es', fields='{"title": ".product-title a", "url": ".product-title a@href", "price": ".price", "original_price": ".regular-price"}', header_text='', item_template='', limit=40, output_format='json', selector='article.product-miniature')


def node_descargar_outletpc(ctx, inputs):
    return runtime.http_call(ctx, inputs, auth_header='Authorization', body='', engine='auto', headers='', impersonate='chrome', method='GET', secret='', timeout=15.0, url='https://outlet-pc.es/collections/juegos-de-mesa?sort_by=created-descending', user_agent='')


def node_extraer_outletpc(ctx, inputs):
    return runtime.html_extract(ctx, inputs, base_url='https://outlet-pc.es', fields='{"title": "h3", "url": "a@href", "price": ".text-price", "original_price": ".line-through"}', header_text='', item_template='', limit=40, output_format='json', selector='div.hover-item')


def node_descargar_zacatrus(ctx, inputs):
    return runtime.http_call(ctx, inputs, auth_header='Authorization', body='', engine='auto', headers='', impersonate='chrome', method='GET', secret='', timeout=15.0, url='https://zacatrus.es/juegos-de-mesa.html?dir=desc&order=news_from_date', user_agent='')


def node_extraer_zacatrus(ctx, inputs):
    return runtime.html_extract(ctx, inputs, base_url='https://zacatrus.es', fields='{"title": ".product-item-name a", "url": ".product-item-name a@href", "price": ".price"}', header_text='', item_template='', limit=40, output_format='json', selector='li.product-item')


def node_combinar_y_comparar(ctx, inputs):
    def _cuerpo(entrada, ctx):
        import datetime, re

        items_m = entrada.get('extraer_mathom') or []
        items_o = entrada.get('extraer_outletpc') or []
        items_z = entrada.get('extraer_zacatrus') or []

        for x in items_m: x['store'] = 'Mathom'
        for x in items_o: x['store'] = 'Outlet-PC'
        for x in items_z: x['store'] = 'Zacatrus'

        all_items = items_m + items_o + items_z

        def clean_game_name(title: str) -> str:
            t = (title or '').strip()
            # Eliminar prefijos de editoriales o marcas comunes
            t = re.sub(r'^(?:ASMODEE|Games Workshop|Mattel Games|Ravensburger|Devir|Hasbro Gaming|Topi Games|Maldito Games|SD Games|CRAZE)\s+', '', t, flags=re.IGNORECASE)
            # Eliminar coletillas de estado o idioma en Outlet-PC y tiendas
            t = re.sub(r'(?i)\s*(?:embalaje\s+deteriorado|reacondicionado|caja\s+abierta|instrucciones\s+en\s+\w+|version\s+\w+|edicion\s+en\s+\w+)', '', t)
            t = re.sub(r'(?i)\s*juego\s+(?:de\s+mesa|de\s+cartas|familiar|de\s+estrategia|estrategico).*', '', t)
            t = re.sub(r'\s*\([^)]*(?:español|castellano|aleman|ingles|frances|expansion|cartas|piezas)[^)]*\)', '', t, flags=re.IGNORECASE)
            return re.sub(r'\s+', ' ', t).strip(' -:,¡!')

        def normalize_title(text: str) -> str:
            t = text.lower().strip()
            for a, b in (('á', 'a'), ('é', 'e'), ('í', 'i'), ('ó', 'o'), ('ú', 'u'), ('ñ', 'n'), ('ü', 'u')):
                t = t.replace(a, b)
            t = re.sub(r'[^\w\s]', '', t)
            return re.sub(r'\s+', ' ', t).strip()

        def matches_game(name1: str, name2: str) -> bool:
            n1, n2 = normalize_title(name1), normalize_title(name2)
            if not n1 or not n2:
                return False
            if n1 == n2:
                return True
            w1, w2 = set(n1.split()), set(n2.split())
            stopwords = {'de', 'la', 'el', 'en', 'y', 'a', 'los', 'las', 'un', 'una', 'con', 'para', 'por', 'del', 'the', 'edition', 'edicion', 'expansion'}
            w1_sig = w1 - stopwords
            w2_sig = w2 - stopwords
            if not w1_sig or not w2_sig:
                return False
            if w1_sig == w2_sig:
                return True
            intersection = len(w1_sig & w2_sig)
            jaccard = intersection / len(w1_sig | w2_sig)
            subset_ratio = intersection / min(len(w1_sig), len(w2_sig))
            if min(len(w1_sig), len(w2_sig)) >= 2:
                return subset_ratio >= 0.8 and intersection >= 2
            return jaccard >= 0.8

        # Agrupar ofertas por juego
        groups = []
        for item in all_items:
            raw_title = item.get('title', '').strip()
            if not raw_title or len(raw_title) < 3:
                continue
            clean_name = clean_game_name(raw_title)
            if not clean_name or len(clean_name) < 3:
                clean_name = raw_title

            found_group = None
            for g in groups:
                if matches_game(clean_name, g['canonical_name']):
                    found_group = g
                    break

            if found_group:
                # Evitar duplicados exactos de URL en la misma tienda
                if not any(o['url'] == item['url'] for o in found_group['offers']):
                    found_group['offers'].append(item)
            else:
                groups.append({'canonical_name': clean_name, 'offers': [item]})

        multi_store_groups = [g for g in groups if len(set(o['store'] for o in g['offers'])) > 1]
        single_store_groups = [g for g in groups if len(set(o['store'] for o in g['offers'])) == 1]

        now = datetime.datetime.now()
        fecha_str = now.strftime('%Y-%m-%d %H:%M')

        lines = [
            f'# 🎲 Novedades y Ofertas de Juegos de Mesa ({fecha_str})',
            '',
            'Compilado diario automatizado (programado a las 12:30) combinando **Mathom**, **Outlet-PC** y **Zacatrus**.',
            f'- **Total productos escaneados:** {len(all_items)} (Mathom: {len(items_m)}, Outlet-PC: {len(items_o)}, Zacatrus: {len(items_z)})',
            f'- **Juegos únicos detectados:** {len(groups)}',
            f'- **Juegos con coincidencia multitienda:** {len(multi_store_groups)}',
            '',
            '---',
            ''
        ]

        if multi_store_groups:
            lines.append('## ⚔️ Comparativa Multitienda (Ofertas coincidentes)')
            lines.append('')
            for g in multi_store_groups:
                lines.append(f'### 🎯 {g["canonical_name"]}')
                for o in g['offers']:
                    pr = o.get('price', '').strip()
                    orig = o.get('original_price', '').strip()
                    price_txt = f'**{pr}**' if pr else 'Consultar precio'
                    if orig and orig != pr:
                        price_txt += f' ~~(antes {orig})~~'
                    lines.append(f'- [{o["store"]}] {price_txt}: [{o["title"]}]({o["url"]})')
                lines.append('')
            lines.append('---')
            lines.append('')

        lines.append('## 📦 Últimas Novedades en Mathom')
        lines.append('')
        for it in items_m[:12]:
            pr = it.get('price', '').strip()
            orig = it.get('original_price', '').strip()
            price_info = f' - **{pr}**' if pr else ''
            if orig and orig != pr:
                price_info += f' ~~(antes {orig})~~'
            lines.append(f'- [{it["title"]}]({it["url"]}){price_info}')
        lines.append('')

        lines.append('---')
        lines.append('')
        lines.append('## 🏷️ Chollos y Reacondicionados en Outlet-PC')
        lines.append('')
        for it in items_o[:12]:
            pr = it.get('price', '').strip()
            orig = it.get('original_price', '').strip()
            price_info = f' - **{pr}**' if pr else ''
            if orig and orig != pr:
                price_info += f' ~~(antes {orig})~~'
            lines.append(f'- [{it["title"]}]({it["url"]}){price_info}')
        lines.append('')

        lines.append('---')
        lines.append('')
        lines.append('## 🌟 Últimas Novedades en Zacatrus')
        lines.append('')
        for it in items_z[:12]:
            pr = it.get('price', '').strip()
            price_info = f' - **{pr}**' if pr else ''
            lines.append(f'- [{it["title"]}]({it["url"]}){price_info}')
        lines.append('')

        return '\n'.join(lines)
    return runtime.script(ctx, inputs, _cuerpo, sources=['extraer_mathom', 'extraer_outletpc', 'extraer_zacatrus'])


def node_guardar_compilado(ctx, inputs):
    return runtime.file(ctx, inputs, content='inputs', directory='', filename='juegos-mesa-ofertas.md', format='raw', message='', mode='sobrescribir')


def run(ctx):
    r_descargar_mathom = node_descargar_mathom(ctx, [])
    r_extraer_mathom = node_extraer_mathom(ctx, [r_descargar_mathom]) if r_descargar_mathom.ok else SKIPPED
    r_descargar_outletpc = node_descargar_outletpc(ctx, [])
    r_extraer_outletpc = node_extraer_outletpc(ctx, [r_descargar_outletpc]) if r_descargar_outletpc.ok else SKIPPED
    r_descargar_zacatrus = node_descargar_zacatrus(ctx, [])
    r_extraer_zacatrus = node_extraer_zacatrus(ctx, [r_descargar_zacatrus]) if r_descargar_zacatrus.ok else SKIPPED
    r_combinar_y_comparar = node_combinar_y_comparar(ctx, [r_extraer_mathom, r_extraer_outletpc, r_extraer_zacatrus]) if (r_extraer_mathom.ok and r_extraer_outletpc.ok and r_extraer_zacatrus.ok) else SKIPPED
    r_guardar_compilado = node_guardar_compilado(ctx, [r_combinar_y_comparar]) if r_combinar_y_comparar.ok else SKIPPED
    return runtime.summarize({'descargar_mathom': r_descargar_mathom, 'extraer_mathom': r_extraer_mathom, 'descargar_outletpc': r_descargar_outletpc, 'extraer_outletpc': r_extraer_outletpc, 'descargar_zacatrus': r_descargar_zacatrus, 'extraer_zacatrus': r_extraer_zacatrus, 'combinar_y_comparar': r_combinar_y_comparar, 'guardar_compilado': r_guardar_compilado}, ctx)
