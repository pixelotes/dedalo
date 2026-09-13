# GENERADO POR DÉDALO -- no editar a mano, se regenera desde el grafo.
# dedalo.VERSION = 1
from dedalo import runtime
from dedalo.runtime import SKIPPED


def node_descargar_valenciabonita(ctx, inputs):
    return runtime.http_call(ctx, inputs, auth_header='Authorization', body='', engine='auto', headers='', impersonate='chrome', method='GET', secret='', timeout=15.0, url='https://www.valenciabonita.es/', user_agent='')


def node_extraer_valenciabonita(ctx, inputs):
    return runtime.html_extract(ctx, inputs, base_url='https://www.valenciabonita.es', fields='{"title": "h3 a", "url": "h3 a@href", "date": ".jeg_meta_date a", "excerpt": ".jeg_post_excerpt p"}', header_text='', item_template='', limit=40, output_format='json', selector='article.jeg_post')


def node_descargar_valenciasecreta(ctx, inputs):
    return runtime.http_call(ctx, inputs, auth_header='Authorization', body='', engine='auto', headers='', impersonate='chrome', method='GET', secret='', timeout=15.0, url='https://valenciasecreta.com/', user_agent='')


def node_extraer_valenciasecreta(ctx, inputs):
    return runtime.html_extract(ctx, inputs, base_url='https://valenciasecreta.com', fields='{"title": ".smn-home-card__title", "url": "a@href"}', header_text='', item_template='', limit=30, output_format='json', selector='article.smn-home-card')


def node_combinar_y_deduplicar(ctx, inputs):
    def _cuerpo(entrada, ctx):
        import datetime, re

        vb_items = entrada.get('extraer_valenciabonita') or []
        vs_items = entrada.get('extraer_valenciasecreta') or []

        now = datetime.datetime.now()
        today_day = str(now.day)
        yesterday = now - datetime.timedelta(days=1)
        yesterday_day = str(yesterday.day)

        # Filtrar Valencia Bonita (últimas 24h)
        vb_filtered = []
        for it in vb_items:
            u = it.get('url', '')
            d = it.get('date', '')
            t = it.get('title', '').strip()
            if not t:
                continue
            is_recent = False
            if f'{now.year}/{now.month:02d}/{now.day:02d}' in u or f'{yesterday.year}/{yesterday.month:02d}/{yesterday.day:02d}' in u:
                is_recent = True
            elif d and (d.startswith(today_day + ' ') or d.startswith(yesterday_day + ' ')):
                is_recent = True
            if is_recent:
                it['source'] = 'Valencia Bonita'
                vb_filtered.append(it)

        if not vb_filtered:
            vb_filtered = vb_items[:6]
            for it in vb_filtered:
                it['source'] = 'Valencia Bonita'

        # Filtrar Valencia Secreta
        vs_filtered = []
        for it in vs_items:
            t = it.get('title', '').strip()
            if t and len(t) > 5:
                it['source'] = 'Valencia Secreta'
                vs_filtered.append(it)

        # Normalizar y deduplicar
        def normalize_title(text):
            t = text.lower().strip()
            replacements = (('á', 'a'), ('é', 'e'), ('í', 'i'), ('ó', 'o'), ('ú', 'u'), ('ñ', 'n'))
            for a, b in replacements:
                t = t.replace(a, b)
            t = re.sub(r'[^\w\s]', '', t)
            return re.sub(r'\s+', ' ', t).strip()

        def are_duplicates(t1, t2):
            n1, n2 = normalize_title(t1), normalize_title(t2)
            if n1 == n2:
                return True
            w1, w2 = set(n1.split()), set(n2.split())
            stopwords = {'de', 'la', 'el', 'en', 'y', 'a', 'los', 'las', 'un', 'una', 'con', 'para', 'por', 'del'}
            w1_sig = w1 - stopwords
            w2_sig = w2 - stopwords
            if not w1_sig or not w2_sig:
                return False
            return (len(w1_sig & w2_sig) / min(len(w1_sig), len(w2_sig))) >= 0.8

        deduped_all = []
        seen = []
        for it in vb_filtered + vs_filtered:
            t, u = it.get('title', ''), it.get('url', '')
            if any(u == p.get('url') or are_duplicates(t, p.get('title', '')) for p in seen):
                continue
            seen.append(it)
            deduped_all.append(it)

        fecha_str = now.strftime('%Y-%m-%d %H:%M')
        total_vb = len([x for x in deduped_all if x['source'] == 'Valencia Bonita'])
        total_vs = len([x for x in deduped_all if x['source'] == 'Valencia Secreta'])

        lines = [
            f'# Agenda Cultural de Valencia ({fecha_str})',
            '',
            'Informe generado automáticamente combinando **Valencia Bonita** y **Valencia Secreta**.',
            f'- **Total eventos y artículos extraídos:** {len(deduped_all)}',
            f'- **Artículos Valencia Bonita (últimas 24h):** {total_vb}',
            f'- **Artículos Valencia Secreta:** {total_vs}',
            '',
            '---',
            '',
            '## 🏛️ Valencia Bonita (Últimas 24 Horas)',
            ''
        ]

        vb_deduped = [x for x in deduped_all if x['source'] == 'Valencia Bonita']
        if vb_deduped:
            for x in vb_deduped:
                d = x.get('date', 'Reciente')
                lines.append(f'### [{x["title"]}]({x["url"]})')
                lines.append(f'- **Fecha:** {d}')
                if x.get('excerpt'):
                    lines.append(f'- **Resumen:** {x["excerpt"]}')
                lines.append('')
        else:
            lines.append('_No se encontraron nuevos artículos en las últimas 24 horas._')
            lines.append('')

        lines.append('---')
        lines.append('')
        lines.append('## ✨ Planes y Eventos Destacados (Valencia Secreta)')
        lines.append('')

        vs_deduped = [x for x in deduped_all if x['source'] == 'Valencia Secreta']
        for x in vs_deduped:
            lines.append(f'- [{x["title"]}]({x["url"]})')

        lines.append('')
        return '\n'.join(lines)
    return runtime.script(ctx, inputs, _cuerpo, sources=['extraer_valenciabonita', 'extraer_valenciasecreta'])


def node_guardar_agenda(ctx, inputs):
    return runtime.file(ctx, inputs, content='inputs', directory='', filename='agenda-cultural-valencia.md', format='raw', message='', mode='sobrescribir')


def run(ctx):
    r_descargar_valenciabonita = node_descargar_valenciabonita(ctx, [])
    r_extraer_valenciabonita = node_extraer_valenciabonita(ctx, [r_descargar_valenciabonita]) if r_descargar_valenciabonita.ok else SKIPPED
    r_descargar_valenciasecreta = node_descargar_valenciasecreta(ctx, [])
    r_extraer_valenciasecreta = node_extraer_valenciasecreta(ctx, [r_descargar_valenciasecreta]) if r_descargar_valenciasecreta.ok else SKIPPED
    r_combinar_y_deduplicar = node_combinar_y_deduplicar(ctx, [r_extraer_valenciabonita, r_extraer_valenciasecreta]) if (r_extraer_valenciabonita.ok and r_extraer_valenciasecreta.ok) else SKIPPED
    r_guardar_agenda = node_guardar_agenda(ctx, [r_combinar_y_deduplicar]) if r_combinar_y_deduplicar.ok else SKIPPED
    return runtime.summarize({'descargar_valenciabonita': r_descargar_valenciabonita, 'extraer_valenciabonita': r_extraer_valenciabonita, 'descargar_valenciasecreta': r_descargar_valenciasecreta, 'extraer_valenciasecreta': r_extraer_valenciasecreta, 'combinar_y_deduplicar': r_combinar_y_deduplicar, 'guardar_agenda': r_guardar_agenda}, ctx)
