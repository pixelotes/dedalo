# Filosofía y Arquitectura de Dédalo

## 1. Motivación y Problema de Partida

En el ecosistema actual de automatización de flujos de trabajo (*workflow automation*), herramientas como **n8n**, **Node-RED**, **Apache Airflow** o **Zapier** son los referentes habituales. Sin embargo, adolecen de graves problemas cuando el objetivo es desplegar automatizaciones fiables en servidores pequeños, entornos perimetrales (*edge computing*) o placas de bajo consumo como una **Raspberry Pi 3B/4/Zero 2W**:

- **Consumo masivo de memoria RAM**: Una instancia mínima de n8n o Node-RED consume con frecuencia entre 350 MB y 1.2 GB de memoria, requiriendo a menudo Redis, PostgreSQL y el runtime de Node.js V8.
- **Empaquetado y sobrecarga en frontend**: Miles de paquetes npm en `node_modules`, dependencias vulnerables en la cadena de suministro y procesos de build que devoran CPU.
- **Motores reactivos complejos**: Mantienen grafos vivos en memoria, colas de eventos asíncronas y estados volátiles que dificultan la depuración cuando algo falla.

**Dédalo nació con una premisa radical:**
> *¿Y si un grafo visual no fuera un motor en memoria, sino simplemente una interfaz gráfica para compilar un script de Python determinista, ligero y plano que consuma menos de 30 MB de RAM?*

---

## 2. Principios de Diseño de Dédalo

### 2.1. Compilación a Código Plano (`def run(ctx):`)
En Dédalo, los nodos y aristas que el usuario dibuja en el lienzo visual no son evaluados por un intérprete de grafos en tiempo de ejecución. En su lugar, el compilador (`dedalo/compiler.py`):
1. Realiza una **ordenación topológica** del grafo dirigido acíclico (DAG).
2. Resuelve dependencias y bifurcaciones condicionales.
3. Genera un fichero de código Python puro en `data/flows/<nombre>.py` con una función `run(ctx)`.

```python
def run(ctx):
    # n1: input.rss
    n1 = runtime.rss(ctx, [], url="https://news.ycombinator.com/rss", max_items=10)
    # n2: transform.filter
    n2 = runtime.filter_items(ctx, [n1], contains="Python") if n1 is not SKIPPED else SKIPPED
    # n3: output.telegram
    n3 = runtime.telegram(ctx, [n2], chat_id="12345", secret="tg_token") if n2 is not SKIPPED else SKIPPED
    return {"n1": n1, "n2": n2, "n3": n3}
```

Este enfoque ofrece ventajas inmensas:
- **Velocidad nativa**: La ejecución es código compilado de Python con el rendimiento directo del intérprete de CPython.
- **Trazabilidad total**: Puedes abrir `data/flows/<nombre>.py`, leer exactamente qué hace el flujo, depurarlo con `pdb` o ejecutarlo de forma aislada.
- **Sin estado oculto**: El flujo empieza, ejecuta sus instrucciones y termina. Cero fugas de memoria en segundo plano.

### 2.2. El Centinela de Omisión (`SKIPPED`)
Para gestionar el flujo de control, condiciones y filtros sin introducir estructuras de control complejas ni ramas anidadas infinitas, Dédalo utiliza el objeto centinela **`SKIPPED`**:
- Si un nodo como `transform.threshold` o `transform.filter` determina que los datos no cumplen la condición requerida, no lanza una excepción; simplemente devuelve `SKIPPED`.
- Las expresiones ternarias generadas por el compilador propagan `SKIPPED` por las ramas descendientes con coste de CPU prácticamente nulo.
- Los nodos terminales no se ejecutan y no generan alertas falsas ni llamadas HTTP innecesarias.

### 2.3. Frontend Cero Dependencias (Vanilla JS + SVG Nativo)
El frontend de Dédalo reside en un único fichero JavaScript (`dedalo/static/dedalo.js`) y CSS puro:
- **Sin frameworks**: Ni React, ni Vue, ni Angular, ni Svelte.
- **Sin empaquetadores**: Cero Webpack, Vite, Rollup o npm. El navegador descarga el HTML y JS estático directamente.
- **Lienzo infinito en SVG nativo**: Conexiones con curvas cúbicas Bézier dibujadas por la GPU del navegador, soporte de zoom, arrastre y snapping en tiempo real con 60 FPS estables.
- **Tiempo de carga inferior a 50 milisegundos**.

### 2.4. Filosofía "Algoritmo Primero, IA Sólo Cuando Aporta Valor"
Muchos frameworks modernos fuerzan el uso de LLMs para tareas mundanas (como clasificar una palabra o resumir un párrafo), lo que introduce latencia de red, costes monetarios, alucinaciones y consumo energético desmedido.

Dédalo prioriza nodos algorítmicos deterministas:
- **Deduplicación**: `transform.fuzzy_dedup` (algoritmo Levenshtein/Jaro-Winkler sin LLM).
- **Resumen**: `transform.summarize` (algoritmos clásicos de Luhn, Luhn+ y TextRank).
- **Detección de novedades**: `transform.diff` (cálculo de hashes SHA-256 almacenados en SQLite/disco).
- **Extracción de entidades**: `transform.regex` y `transform.html_extract` (selectores CSS).

La Inteligencia Artificial se reserva para tareas donde el razonamiento no determinista es insustituible (diagnóstico de causas raíz con `transform.analyzer`, planes de mitigación con `transform.planner` o codificación iterativa con `transform.coder`).

---

## 3. Arquitectura del Sistema

```text
┌────────────────────────────────────────────────────────┐
│                   Navegador Web                        │
│   (Vanilla JS + SVG Canvas + CSS Grid/Flexbox)         │
└───────────────────────────▲────────────────────────────┘
                            │ HTTP / JSON API
┌───────────────────────────▼────────────────────────────┐
│              Servidor FastAPI / Starlette              │
│  - /api/flows       - /api/catalog    - /api/secrets   │
│  - /api/run/{flow}  - /api/debug      - /api/export    │
├────────────────────────────────────────────────────────┤
│                   Compilador Dédalo                    │
│   (dedalo/compiler.py - TopoSort + Code Generation)    │
├────────────────────────────────────────────────────────┤
│                 Planificador de Tareas                 │
│   (dedalo/cron.py - Parser cron nativo sin daemon ext) │
├────────────────────────────────────────────────────────┤
│                   Runtime de Nodos                     │
│   (dedalo/runtime.py - 64 funciones modulares)         │
├────────────────────────────────────────────────────────┤
│                   Almacén de Datos                     │
│  - data/flows/*.py (scripts generados)                 │
│  - data/secrets/*  (ficheros 0600)                     │
│  - data/state/*    (estados diff y persistencia)       │
│  - data/history.db (auditoría SQLite)                  │
└────────────────────────────────────────────────────────┘
```

---

## 4. Gestión de Seguridad y Secretos

1. **Aislamiento en Disco con Permisos Restringidos**:
   - Los secretos (tokens de Jira, credenciales de correo, API keys) se almacenan en `data/secrets/<nombre>.secret` con permisos POSIX `0600` (lectura/escritura exclusiva del usuario del proceso).
   - Alternativamente, pueden inyectarse como variables de entorno estándar.
2. **Sin Fugas en el Código Compilado**:
   - El compilador nunca escribe los valores de las credenciales en los ficheros generados `.py`.
   - En el código se genera `secret="nombre_del_secreto"`, y en tiempo de ejecución el runtime invoca `dedalo.secrets.get("nombre_del_secreto")` resolviéndolo en memoria volátil.
3. **Enmascaramiento Dinámico de Telemetría**:
   - Nodos como `transform.analyzer` disponen del parámetro `sanitize_secrets: True`, que aplica expresiones regulares previas para ofuscar tokens JWT, Bearer tokens, claves privadas RSA y contraseñas antes de que cualquier texto salga hacia un LLM o servicio de terceros.
