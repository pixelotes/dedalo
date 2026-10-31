# Dédalo Standalone — Estado del Proyecto, Arquitectura y Relación con Tomos

Este documento resume el estado técnico actual de **Dédalo Standalone**, su origen y sincronización con el proyecto **Tomos**, las decisiones de arquitectura adoptadas y el registro completo de mejoras implementadas. Sirve como referencia base para continuar el desarrollo de Dédalo como proyecto 100% autónomo.

---

## 1. Visión y Estado Actual

**Dédalo** es un orquestador visual de flujos de trabajo ultraligero diseñado específicamente para entornos de recursos limitados (como una Raspberry Pi o pequeños servidores domésticos) sin incurrir en la sobrecarga de herramientas como n8n, Node-RED o Airflow.

### Características Principales en Standalone:
- **Consumo mínimo de RAM**: ~14-16 MB en modo demonio puro (`--daemon`), ~28-34 MB en modo servidor web sin frontend (`--headless`), y ~35-42 MB con servidor web completo en Linux/Raspberry Pi (frente a los más de 400 MB habituales en aplicaciones basadas en Node.js o Electron).
- **Backend**: Python 3.10+ puro con FastAPI, Uvicorn y SQLite en modo WAL.
- **Frontend**: Vanilla JavaScript (ES6+) y SVG nativo. Sin empaquetadores (Webpack/Vite), sin frameworks pesados (React/Vue).
- **Compilación a Python plano**: Los grafos visuales no se interpretan paso a paso en un bucle reactivo; se compilan a un script Python determinista (`<slug>.py`) con operadores ternarios y el centinela `SKIPPED`.
- **Evaluador de Cron Nativo**: Parser de cron e intervalos (`@every 30m`, `@hourly`, etc.) 100% en Python puro sin dependencias externas (`croniter` y `python-dateutil` eliminadas).
- **Lienzo Infinito**: Sistema de transformación matricial con paneo por arrastre, zoom por rueda o controles en pantalla y coordenadas de mundo desacopladas.
- **Conexiones en bordes**: Puertos de entrada en el borde lateral izquierdo y bifurcaciones de éxito/fallo (`ok` / `error`) en el borde lateral derecho.
- **Historial de ejecuciones**: Base de datos SQLite integrada con rotación automática periódica cada 24 horas (elimina ejecuciones con más de 30 días).
- **Control de cambios**: Detección de modificaciones no guardadas con modal de confirmación antes de cambiar de flujo.
- **Optimización de Memoria Activa**: Compactación periódica de heap con recolección de basura forzada y devolución de páginas al OS vía `malloc_trim(0)`.
- **Evasión Antibot sin Navegador Pesado**: Motor HTTP dual con `curl_cffi` (suplantación de TLS/HTTP/2 de Chrome/Safari/Firefox y User-Agents) y fallback transparente a `httpx`.
- **Suite de pruebas**: 104 pruebas automatizadas pasando al 100% en `pytest`.

---

## 2. Relación con Tomos

### Origen
Dédalo nació originalmente como el módulo de automatización y habilidades visuales dentro del proyecto **Tomos** (`src/tomos/dedalo*.py` y `src/tomos/static/dedalo.*`). En Tomos, su propósito era permitir al usuario crear flujos que consumiesen los índices de búsqueda, resúmenes del BOE y agentes de lenguaje.

### Independización
Se decidió extraer Dédalo como un repositorio independiente (`dedalo`) para:
1. Poder utilizarlo como un servicio de automatización de propósito general en cualquier máquina o Raspberry Pi sin instalar las dependencias de Tomos (procesamiento de PDFs, embeddings locales, scrapers masivos, etc.).
2. Dotarlo de un ciclo de vida, configuración, secretos e historial propios.
3. Permitir que cualquier persona pueda desplegarlo con un simple `git clone` y `./run.sh`.

### Compatibilidad y Sincronización (Backporting)
A pesar de la separación, Dédalo mantiene compatibilidad a nivel de datos y modelo conceptual con Tomos:
- **Esquema JSON**: El formato de serialización de grafos (`nodes`, `edges`, propiedades de configuración) es idéntico. Cualquier flujo exportado desde Dédalo puede copiarse a `data/skills/` en Tomos.
- **Patrón de Compilación**: El compilador genera el mismo código Python basado en centinelas `SKIPPED` y `entrada["nodo"]`.
- **Diseño Visual de Nodos**: La disposición de conectores (entrada a la izquierda, salida `ok` a 33% a la derecha, salida `error` a 67% a la derecha) y las curvas bézier horizontales son idénticas al estándar visual probado en Tomos.

### Diferencias Clave de la Versión Standalone frente a Tomos
| Aspecto | Tomos (Módulo interno) | Dédalo Standalone |
| :--- | :--- | :--- |
| **Servidor** | Integrado en la app web de Tomos | Servidor FastAPI autónomo en puerto configurable (8080) |
| **Gestión de Secretos** | Hereda variables de entorno de Tomos | Almacén propio cifrado/protegido en `data/secrets.json` con permisos `0600` |
| **Historial** | No persistido o en memoria efímera | SQLite con WAL (`data/history.db`) y purga periódica a 30 días |
| **Lienzo** | Posición estática con scroll de contenedor | Paneo fluido por arrastre de fondo y zoom libre con controles |
| **Paleta de Nodos** | Selección básica por clic | Clic o arrastre nativo (`HTML5 Drag & Drop`) al punto exacto |
| **Inspector Lateral** | Fijo en panel derecho | Colapsable con botón y atajo, persistente en `localStorage` |
| **Editor de Código** | Textarea simple en barra lateral | Editor CodeMirror modal a pantalla completa con visor de variables |
| **Catálogo de Nodos** | Enfocado en tareas documentales y Tomos | Ampliado con 14 nodos de sistema (SQLite, Webhooks, Raspberry Pi, Discord, Telegram, etc.) |

---

## 3. Decisiones Técnicas y de Arquitectura

1. **Cero Dependencias de Construcción (No-Build)**:
   Todo el frontend se entrega como archivos estáticos directos (`index.html`, `dedalo.js`, `dedalo.css`). No se requiere Node.js, npm, ni pasos de compilación para la UI. Esto garantiza despliegues instantáneos y máxima longevidad del código sin problemas de dependencias obsoletas.

2. **Espacio de Coordenadas de Mundo en el Canvas**:
   El estado interno del flujo guarda las coordenadas de cada nodo (`node.x`, `node.y`) en un sistema de coordenadas de mundo continuo. La capa visual aplica una única matriz de transformación CSS (`transform: translate(...) scale(...)`) al contenedor `#canvasContent`. Todas las interacciones del ratón (cables, arrastre de nodos, soltado desde la paleta) se traducen con la función `screenToWorld(clientX, clientY)`, haciendo que el pan y zoom nunca desalineen los elementos ni los cables.

3. **Cálculo Determinista de Curvas SVG (`portCentre` & `curve`)**:
   Los cables no son líneas rectas ni curvas SVG arbitrarias. Siguen una fórmula bézier cúbica horizontal (`M x1 y1 C x1+dx y1, x2-dx y2, x2 y2`) con tensión adaptativa (`Math.max(38, dx * 0.45)`). Los puntos de anclaje se calculan respecto a los bordes del nodo emisor y receptor, garantizando que el flujo visual se lea siempre de izquierda a derecha de forma natural.

4. **Persistencia Ligera en SQLite con WAL**:
   Para registrar cada ejecución sin degradar el rendimiento de la tarjeta SD en una Raspberry Pi:
   - Se activa el modo Write-Ahead Logging (`PRAGMA journal_mode=WAL`).
   - Se utiliza `PRAGMA synchronous=NORMAL`.
   - Se ejecutan consultas parametrizadas directas con el módulo nativo `sqlite3` de Python.
   - Un hilo en segundo plano revisa cada 24 horas y purga los registros anteriores a 30 días.

5. **Triggers Reactivos mediante Webhooks**:
   Cualquier flujo que comience con un nodo `input.webhook` queda expuesto de inmediato en el endpoint `/api/webhook/{flow_id}`. Al recibir una petición HTTP (POST, GET o PUT), el servidor compila y ejecuta el flujo en segundo plano pasando el cuerpo, encabezados y parámetros de consulta como datos de entrada.

---

## 4. Registro de Mejoras Implementadas

A continuación se detallan las mejoras realizadas sobre la base standalone:

### A. Interfaz y Experiencia de Usuario (UI/UX)
- **Conectores Laterales Estilo Tomos**:
  - Puerto de entrada (`.port.in`) centrado en el borde izquierdo (`left: -7px; top: 50%;`).
  - Puertos de salida en el borde derecho: éxito (`.port.ok`, verde) al 33% y error (`.port.error`, rojo) al 67%.
  - Etiquetas descriptivas `ok` y `error` (`.port-tag`) a la derecha de los conectores.
  - Eliminada la barra interior inferior (`.node-ports`), dejando el cuerpo del nodo más amplio y limpio.
- **Conexión Amigable por Arrastre**:
  - Al arrastrar un cable, los puertos de entrada disponibles emiten una animación sutil de pulso (`is-wiring`).
  - Posibilidad de soltar el cable sobre el conector o sobre **cualquier parte del nodo destino** (`document.elementFromPoint`).
- **Drag & Drop desde la Paleta Izquierda**:
  - Las píldoras de la barra lateral (`.node-pill`) son arrastrables (`draggable="true"`).
  - Al soltarlas en el lienzo, el nuevo nodo se crea exactamente en la posición del cursor traducida al espacio de mundo. Hacer clic sobre la píldora lo coloca en el centro visible actual.
- **Código de Color por Categorías**:
  - Píldoras del menú lateral con acentos y fondos según su tipo: **Entrada** (azul cielo `#38bdf8`), **Transformación** (violeta `#c084fc`) y **Salida** (verde esmeralda `#34d399`).
  - Cabecera del nodo en el canvas (`.node-header`) tintada con el color de su categoría.
- **Inspector Lateral Colapsable**:
  - Botón de cierre en la cabecera del panel (`#btnCollapseInspector`), botón en la barra superior (`#btnToggleInspector`) y pestaña flotante en el lienzo para restaurarlo.
  - Expansión automática al hacer doble clic sobre cualquier nodo.
  - Persistencia de la preferencia en `localStorage`.
- **Editor de Código Modal a Pantalla Completa**:
  - Para nodos de script Python (`transform.script`), el inspector ofrece un botón para abrir un modal con editor CodeMirror vendorizado, resaltado de sintaxis, atajos de teclado y visualizador de variables de entrada.
- **Navegación y Pan/Zoom**:
  - Paneo fluido arrastrando el fondo del lienzo (con botón izquierdo o central).
  - Zoom mediante rueda del ratón centrado en el cursor.
  - Barra de herramientas con botones de Zoom In, Zoom Out, 100% y Centrar Flujo en pantalla.
- **Gestión de Cambios sin Guardar (Estado Dirty)**:
  - Indicador de estado modificado (`isDirty`) con badge visual pulsante (`● Sin guardar`) en la barra superior junto al nombre del flujo.
  - Actualización reactiva del título de la pestaña (`● nombre-del-flujo — Dédalo`) y resplandor contextual en el botón de guardar (`#btnSave.is-dirty`).
  - Diálogo modal de confirmación antes de cambiar a otro flujo o crear uno nuevo si hay cambios pendientes.
- **Atajos de Teclado y Ergonomía Nativa**:
  - `Cmd+S` / `Ctrl+S`: Guardar flujo desde cualquier contexto (incluso editando texto o código).
  - `Delete` / `Backspace`: Eliminar el nodo seleccionado directamente (respetando inputs y modales).
  - `Cmd+D` / `Ctrl+D`: Duplicar el nodo seleccionado con desplazamiento automático y configuración clonada.
  - `Escape`: Deseleccionar nodo, cancelar cableado en curso o cerrar cualquier modal abierto.
  - `Barra Espaciadora (Space + Arrastre)`: Paneo libre y fluido del lienzo con cursor de mano (`grab` / `grabbing`).
- **Jerarquía y Reorganización de la Barra Superior (Header)**:
  - Agrupación funcional por bloques semánticos con separadores sutiles: *Flujo* (`[Vaciar 🗑 | Plantillas... ▾]`), *Herramientas* (`[Código] [Programar]`), *Ejecución* (`[Probar ▶ | Depurar 🪲]`), *Acción Principal* (`[Guardar]`) y *Entorno* (`[Inspector] [Tema]`).
  - Campo de nombre de flujo responsivo con ancho adaptativo para evitar truncado de nombres descriptivos.
- **Paleta de Nodos Optimizada (Chips + Acordeón)**:
  - Chips de filtrado rápido superior (`Todos`, `Red/Web`, `Datos`, `IA`, `Git`, `Sistema`) para filtrar al instante los 56 nodos.
  - Bloques de categoría colapsables (Entrada, Transformación, Salida) con contador dinámico de nodos y chevron interactivo.
  - Despliegue automático de secciones al realizar búsquedas en tiempo real.
- **Nodos Limpios sin Ruido Visual**:
  - Eliminadas las etiquetas de texto flotantes `ok` y `error` del borde derecho de los nodos para evitar solapamientos con cables vecinos; la semántica se comunica de forma intuitiva por el color del puerto (esmeralda para éxito, rojo para fallo) y tooltips nativos.
- **Corrección de Visibilidad y Comportamiento Toggle en Barra de Depuración**:
  - Resuelto conflicto de especificidad CSS con el atributo `[hidden]`, garantizando que la barra de depuración paso a paso y el visor modal de datos permanezcan ocultos por defecto hasta que el usuario los invoque deliberadamente.
  - El botón superior **Depurar** (`#btnDebugFlow`) funciona ahora como un interruptor (*toggle*): un primer clic activa el modo depuración y abre la barra, mientras que un segundo clic cierra la barra y finaliza la sesión.
  - El botón adopta un estado activo persistente en color azul primario (`.active`) mientras el modo de depuración está activo en el lienzo, recuperando su estilo secundario habitual al cerrarse o reiniciarse el lienzo.
  - Barra de depuración compacta basada exclusivamente en iconos (`icon-btn` con SVG nativos para Reiniciar, Siguiente paso, Continuar y Detener) y nombre de nodo adaptable con truncado elíptico.
  - Al avanzar con **Siguiente paso**, el inspector lateral muestra automáticamente los datos del último nodo recién ejecutado (entradas, salidas y tiempo de ejecución). Al mismo tiempo, el usuario tiene plena libertad para hacer clic en cualquier otro nodo del flujo y examinar sus datos sin que el depurador fuerce la vista.

### B. Historial de Ejecuciones
- Pestaña dedicada en la barra lateral con listado histórico de ejecuciones, fecha/hora, duración y estado (`OK`, `ERR`, `RUN`).
- Hoja modal de detalle para inspeccionar las entradas y salidas de cada nodo individual.
- Resaltado visual en el propio lienzo del resultado de la última prueba (`node-run-ok`, `node-run-err`, `node-run-skip`).
- Purga automática cada 24h para descartar ejecuciones de más de 30 días.

### C. Catálogo de Nodos Ampliado (14 Nuevos Tipos Ultraligeros)
Sin añadir dependencias pesadas y aprovechando la librería estándar de Python:
- **Entradas**: `input.webhook` (disparador HTTP), `input.sqlite_query` (consultas SQL locales), `input.command` (ejecución shell controlada), `input.pi_health` (telemetría de CPU/RAM de Raspberry Pi), `input.directory_scan` (escaneo de ficheros), `input.http_health` (comprobación de latencia/estado web).
- **Transformaciones**: `transform.html_extract` (selector CSS y extracción de datos HTML sin dependencias), `transform.template` (sustitución de plantillas con variables), `transform.array_ops` (ordenar, filtrar, recortar listas), `transform.regex` (extracciones y reemplazos con expresiones regulares), `transform.router` (bifurcador condicional a puerto `ok` o `error`).
- **Salidas**: `output.sqlite_exec` (escritura en base de datos local), `output.email_smtp` (envío de correos vía SMTP), `output.ntfy` (notificaciones push a móviles vía ntfy.sh), `output.discord` (webhooks con mensajes y embeds), `output.slack` (notificaciones para equipos), `output.command` (ejecución de comandos de sistema de salida).

### D. Optimización Extrema de Memoria y Recursos (3 Niveles Implementados)
Diseñado para operar indefinidamente en servidores pequeños y Raspberry Pis con bajo consumo garantizado:
1. **Nivel 1: Ajustes Inmediatos de SQLite y Concurrencia**:
   - Limitación estricta de caché de SQLite en `dedalo/history.py`: `PRAGMA cache_size = -2000` (máximo 2 MB en lugar del default de hasta decenas de MB), `PRAGMA mmap_size = 0` (evita mapeo agresivo en memoria virtual), `PRAGMA temp_store = FILE`.
   - Banderas de Uvicorn en `run.sh`: `--limit-concurrency 15`, `--timeout-keep-alive 5`, `--no-access-log`.
2. **Nivel 2: Eliminación de Dependencias, Modos Headless y Compactación Activa**:
   - **Evaluador de Cron Nativo (`dedalo/cron.py`)**: Reemplazo total de la librería externa `croniter` (y su dependencia pesada `python-dateutil`). Ahorro directo: **~5.4 MB de RAM** en tiempo de carga. Soporta intervalos `@every 30m`, shortcuts `@hourly`, `@daily` y sintaxis clásica de 5 campos cron.
   - **Compactador de Heap (`dedalo/runtime.py:compact_memory`)**: Ejecuta `gc.collect()` forzado y llama a `libc.malloc_trim(0)` en sistemas Linux para devolver páginas no fragmentadas al sistema operativo de inmediato tras cada ejecución de flujo y en cada ciclo del planificador.
   - **Modo Web Headless (`./run.sh --headless`)**: Omite el montaje de ficheros estáticos y la UI web. Mantiene la API REST y webhooks activos. Consumo en reposo: **~28-34 MB** en Linux.
   - **Modo Demonio Puro (`./run.sh --daemon` / `python -m dedalo.headless`)**: Ejecuta únicamente el planificador de flujos (`Scheduler`) sin Uvicorn, FastAPI ni Starlette. Consumo en reposo: **~14-16 MB** en Linux.
3. **Nivel 3: Aislamiento a Nivel de Sistema Operativo y Contenedores**:
   - Banderas de entorno glibc en `run.sh`: `MALLOC_TRIM_THRESHOLD_=65536` y `PYTHONOPTIMIZE=1`.
   - Perfil de servicio `systemd` documentado en `README.md` con límites `MemoryHigh=65M` y `MemoryMax=85M`.
   - **Empaquetado Docker Endurecido (`Dockerfile` + `docker-compose.yml`)**: Construcción multi-stage, usuario no privilegiado (`UID/GID 1000`), `cap_drop: ALL`, `no-new-privileges: true`, healthcheck nativo en Python (sin curl), montaje de `/tmp` en `tmpfs` y control estricto de memoria (límite 128 MB).

### E. Recuperación Web con Evasión Antibot y TLS (`curl_cffi`)
Para evitar los cientos de megabytes de consumo y picos de CPU de un navegador headless como Obscura, se ha integrado soporte nativo para **`curl_cffi`** (basado en `libcurl-impersonate`):
- **Suplantación criptográfica TLS y HTTP/2**: Replica la huella exacta de navegadores reales (Chrome, Safari, Firefox) eludiendo los bloqueos típicos de Cloudflare, Akamai y Datadome con solo ~2-4 MB de memoria.
- **Nodos beneficiados**:
  - `input.http_call` (peticiones REST y scraping genérico).
  - `input.web_read` (descarga y limpieza de páginas web).
  - `input.rss` (descarga de feeds protegidos por Cloudflare o Substack).
- **Selector de Motor**: `auto` (usa `curl_cffi` con fallback automático a `httpx`), `curl_cffi` o `httpx`.
- **User-Agent configurable**: Selección de presets (`chrome`, `safari`, `firefox`, `mobile`, `bot`) o User-Agent personalizado libre.
- **Fallback resiliente**: Si una petición en modo `auto` encuentra un fallo en `curl_cffi`, se intenta automáticamente mediante `httpx` antes de reportar un error.

### F. Resumen Estadístico Avanzado (Luhn, Luhn+ y TextRank)
Se ha potenciado el nodo de resumen determinista sin LLM (`transform.summarize`):
- **Cuatro Motores Disponibles**:
  - **`luhn+`** (por defecto): Filtro de más de 120 stopwords (español e inglés), ponderación por densidad informativa normalizada por longitud ($\sqrt{N}$), sesgo de posición (*Lead Bias* +50% a la frase definitoria de apertura) y selección por diversidad (MMR ligero que penaliza la redundancia temática).
  - **`textrank`**: Implementación nativa en Python puro del algoritmo clásico de Mihalcea & Tarau (2004). Construye un grafo de similitud léxica entre oraciones y calcula su centralidad mediante iteraciones de PageRank amortiguado ($d=0.85$).
  - **`luhn`**: Algoritmo clásico de frecuencia simple para compatibilidad regresiva.
  - **`llm`**: Resumen generativo mediante API OpenAI/compatible.
- **Formato de Salida Flexible (`format`)**:
  - `paragraph`: Texto continuo clásico en un solo párrafo.
  - `bullets`: Lista Markdown con viñetas (`- Frase`), ideal para digerir puntos clave sin chirridos sintácticos.
- **Límites Configurables**: Soporte para número exacto de oraciones (`sentences: int`) o ratio porcentual del documento (`ratio: float`), complementando los presets clásicos (`short`, `medium`, `long`, `full`).
- **Segmentación de Oraciones Robusta**:
  - Protección de abreviaturas y títulos comunes en español/inglés (`Dr.`, `Dra.`, `Sr.`, `p. ej.`, `art.`, `a. C.`, `c.`, `ca.`, `EE. UU.`, etc.) para evitar fragmentaciones indebidas por puntos internos.
  - Limpieza automática de encabezados de sección wiki (`== Título ==`) y markdown (`###`).
  - Filtrado de líneas residuales o llamadas a pie de página sin perder coherencia sintáctica.
- **Acceso a Estructuras Anidadas Complejas**:
  - Tanto `transform.summarize` como `transform.template` admiten ahora navegación dot-notation con índices de listas (ej: `query.pages.0.extract`).
  - Búsqueda recursiva automática como mecanismo de respaldo si una clave no se encuentra en el nivel raíz.

### G. Búsqueda Web Multifuncional (`input.web_search`)
Se ha incorporado un nodo de búsqueda web nativo que permite alimentar cualquier flujo con información en vivo de internet sin dependencias pesadas:
- **Tres Motores Integrados**:
  - **`duckduckgo`** (por defecto): Cero configuración, gratuito y sin necesidad de claves de API. Realiza scraping limpio con evasión TLS (`curl_cffi`/`httpx`), extrayendo títulos, snippets descriptivos y decodificando las URLs reales de destino (desempaquetando el parámetro `uddg`). Desescapa entidades HTML automáticamente.
  - **`tavily`**: Diseñado específicamente para agentes e IA (`https://api.tavily.com/search`). Requiere conectar un secreto seguro con la API Key. Devuelve snippets contextuales enriquecidos y puntuaciones de relevancia (`score`).
  - **`exa`** (Metaphor): Búsqueda neuronal y semántica (`https://api.exa.ai/search`). Requiere conectar un secreto seguro con la API Key (`x-api-key`). Devuelve snippets, destacados textuales (`highlights`) y fecha de publicación.
- **Interpolar Consultas Dinámicas**:
  - Soporta plantillas con `{entrada}` para buscar términos generados dinámicamente por nodos previos (RSS, transformaciones, LLM, etc.). Si `query` está vacío, utiliza el texto del nodo conectado automáticamente.
- **Formatos de Salida Flexibles (`output_format`)**:
  - `json`: Lista estructurada de objetos Python/JSON con `title`, `url`, `snippet` (y metadatos como `score` o `published_date`).
  - `markdown`: Lista formateada con viñetas y enlaces Markdown (`- [Título](URL): Snippet`), lista para encadenar a `transform.summarize`, `output.file` o nodos de mensajería (Telegram, Discord, Slack, Email).
- **Integración con la UI y Secretos de Dédalo**:
  - El campo `secret` se vincula dinámicamente al almacén de secretos (`choices_from={"secret": "secrets"}`) y se muestra condicionalmente solo cuando se selecciona un motor comercial que lo requiere (`show_when={"secret": {"engine": ["tavily", "exa"]}}`).

### H. Nodos Deterministas sin LLM y Buscador de la Paleta
Para garantizar la máxima autonomía, predictibilidad y rendimiento en Raspberry Pi (~20 MB RAM) sin incurrir en costes de API ni latencias de modelos de lenguaje, se ha integrado una batería completa de nodos deterministas basados 100% en la librería estándar de Python:
- **Detección de Cambios y Novedades (`transform.diff`)**:
  - Conserva una instantánea de estado en `data/state/{flow}_{key}.json` entre ejecuciones sucesivas.
  - Permite emitir solo elementos nuevos (`added`), modificados (`modified`), eliminados (`removed`) o el resumen global (`all`).
  - Previene que los flujos periódicos vuelvan a notificar elementos ya vistos en días anteriores.
- **Deduplicación Difusa Inteligente (`transform.fuzzy_dedup`)**:
  - Combina `difflib.SequenceMatcher` con similitud de conjuntos léxicos (Jaccard y contención de subconjuntos) para unificar títulos con ligeras variaciones entre tiendas o periódicos.
  - Modos `keep_first`, `merge_list` (adjunta la lista `duplicates`) y `group` (clusters).
- **Agregador Estadístico (`transform.aggregate`)**:
  - Calcula `count`, `sum`, `avg`, `min`, `max`, `median` y `summary` completo sobre campos numéricos, con soporte de agrupación por clave (`group_by`).
- **Formateador de Tablas (`transform.table`)**:
  - Renderiza listas de diccionarios en tablas Markdown alineadas o HTML con estilos inline, listo para envíos de correo o notificaciones visuales.
- **Extracción de Palabras Clave RAKE (`transform.keywords_rake`)**:
  - Algoritmo *Rapid Automatic Keyword Extraction* implementado en Python puro para extraer conceptos clave y temáticas de textos sin modelos NLP pesados.
- **Vigilancia de Infraestructura y Red (`input.ssl_cert`, `input.dns_check`, `input.ping`)**:
  - `ssl_cert`: Días restantes de validez, emisor, sujeto y estado de certificados TLS (`ssl` y `socket`).
  - `dns_check`: Resolución de direcciones IP y tiempo de respuesta DNS.
  - `ping_host`: Latencia de conexión en ms vía TCP handshake (sin requerir privilegios root ni raw sockets en Docker/Linux) o ping ICMP nativo.
- **Conectividad Domótica e IoT (`output.mqtt` / `input.mqtt`)**:
  - Cliente nativo de protocolo binario MQTT 3.1.1 implementado sobre sockets directos de Python (cero librerías externas necesarias). Publica y lee mensajes en brokers locales como Mosquitto o Home Assistant.
- **Lector de Correo IMAP (`input.imap`)**:
  - Conexión segura IMAP sobre TLS (`imaplib` + `email`) con búsqueda por filtros (`UNSEEN`, `ALL`, etc.), decodificación de cabeceras MIME y extracción de cuerpo y adjuntos.
- **Buscador en Vivo en la Paleta de Nodos**:
  - Campo `#paletteSearch` en la pestaña de nodos de la barra lateral que filtra instantáneamente por nombre, tipo, categoría o descripción conforme se escribe, ocultando categorías vacías.

### I. 5 Nodos de Alta Utilidad Inspirados en n8n (52 Nodos en Total)
- **Pausa y Espera con Jitter (`transform.delay`)**:
  - Detiene el flujo durante `N` segundos (con opción de variación aleatoria `jitter` para eludir patrones mecánicos al scrapear webs) y reenvía los datos sin alterar.
- **Operaciones de Fecha y Hora (`transform.datetime`)**:
  - Cálculo, formateo en español (`Domingo, 13 de Septiembre...`), sumas/restas de días u horas (`offset_days`, `offset_hours`), zona horaria IANA con `zoneinfo` nativo y modos de salida en texto o diccionario estructurado.
- **Lector de Sitemap XML (`input.sitemap`)**:
  - Descarga y procesa archivos `sitemap.xml` (incluyendo índices anidados `<sitemapindex>`), filtrando por antigüedad en horas (`filter_hours`) o palabras clave en la URL.
- **Ping de Monitorización Heartbeat (`output.healthcheck`)**:
  - Emite peticiones GET o POST de confirmación a Healthchecks.io (`/fail` automático si hubo error) o Uptime Kuma Push (`?status=up&msg=OK`), permitiendo vigilar la salud de los crons.
- **Meteorología Abierta (`input.weather`)**:
  - Consulta en tiempo real a Open-Meteo (100% libre, sin registro ni claves API) con geocodificación automática por nombre de ciudad, previsión a varios días y códigos WMO traducidos al español con emojis.

### J. Exportación e Importación de Flujos en ZIP y Backup Completo
Para facilitar la portabilidad, el despliegue entre diferentes máquinas (ej. estación de desarrollo a Raspberry Pi) y copias de seguridad sin dependencias de compilación:
- **Exportación Individual en ZIP**:
  - Genera al vuelo un archivo ZIP en memoria conteniendo el grafo visual canónico (`<flow_id>.json`), su metadata cron (`<flow_id>.meta.json`) y un manifiesto (`manifest.json`).
  - **Detección de Secretos Requeridos**: Inspecciona los nodos del flujo y añade al manifiesto los nombres de los secretos requeridos para funcionar (ej. `TAVILY_API_KEY`, `TELEGRAM_BOT_TOKEN`), sin incluir en ningún caso sus valores confidenciales.
- **Copia de Seguridad Completa (Backup All)**:
  - Endpoint `GET /api/flows/export-all` y botón en cabecera de la UI que empaqueta todos los flujos del sistema en `dedalo_flows_backup_<timestamp>.zip`.
- **Importación Inteligente y Segura**:
  - Endpoint `POST /api/flows/import` y selector de archivos en la barra lateral con soporte para archivos `.zip` y `.json`.
  - **Cero Dependencias**: Implementado con módulos estándar de Python (`zipfile`, `io`) y analizador multipart nativo para evitar la dependencia de `python-multipart`.
  - **Seguridad Zip-Slip**: Sanitización exhaustiva de rutas relativas con rechazo estricto de secuencias de escape de directorio (`..`).
  - **Protección contra DoS**: Límite de descompresión de 25 MB.
  - **Recompilación Automática**: Guarda el grafo e invoca `save_flow()`, compilándolo de inmediato a Python ejecutable (`<flow_id>.py`).

### K. Depuración Paso a Paso, Inspección Completa y Transformaciones en Tiempo Real
Para diagnosticar flujos de datos complejos y experimentar con transformaciones interactivamente sin sobrecargar servicios remotos:
- **Depuración Paso a Paso en Canvas**:
  - Barra de herramientas flotante (`#debugToolbar`) con control de estado (nodo actual, siguiente paso, continuar, reiniciar y detener).
  - Resaltado visual en el lienzo con animación de pulso cian (`.node-debug-active`) sobre el nodo en depuración.
  - Ejecución dirigida: botón "▶ Ejecutar hasta este nodo" en el inspector de propiedades para evaluar el subgrafo DAG necesario hasta un nodo arbitrario.
  - Endpoints dedicados: `POST /api/flow/step` (con modos `step` y `run_to`) y función backend `runtime.execute_single_node()`.
- **Inspección de Datos No Truncados**:
  - En modo depuración e inspección, se desactiva la truncación de datos (`truncate=False`), preservando entradas y salidas al 100% de fidelidad.
  - El inspector de nodos incluye pestañas `[Salida]` y `[Entrada]`, visualización conmutada Formateado / Raw y botón de copia rápida al portapapeles.
  - Modal `#dataViewerModal` a pantalla completa para inspección profunda de datos JSON/HTML/Texto con buscador por líneas, contador de filas y cálculo de tamaño en KB/MB.
- **Transformaciones en Tiempo Real (Live Preview Playground)**:
  - Panel interactivo en el inspector para nodos de transformación (`transform.*`).
  - Al modificar parámetros (expresiones regulares, selectores CSS, formatos de fecha, scripts Python, etc.), el resultado se recalcula en tiempo real (~2-4 ms) con debounce de 250ms.
  - Toma automáticamente como entrada los datos reales producidos por el nodo padre en la última ejecución/paso o permite alternar a una muestra personalizada de prueba.

### L. Procesamiento Seguro de Correo, Sanitización Anti-Inyecciones y Resumen Diario (53 Nodos en Total)
Para automatizar la lectura y análisis de emails de forma robusta frente a vectores de ataque modernos sin dependencias pesadas:
- **Sanitización Profunda Anti Prompt-Injection**:
  - `EmailSanitizerParser` nativo en Python estándar (`html.parser`): suprime scripts, estilos, metadatos, comentarios HTML y elementos con trucos de ocultación CSS (`display:none`, `visibility:hidden`, `opacity:0`, `font-size:0`, `color:transparent`, `color:#fff`, `text-indent:-9999px`, `aria-hidden="true"`, `hidden`).
  - Filtro de caracteres Unicode invisibles (`_INVISIBLE_CHARS_RE`): elimina zero-width spaces/joiners (`\u200b`, `\u200c`, `\u200d`, `\u2060`, `\ufeff`), soft hyphens (`\u00ad`), planos de etiquetas Unicode (`\U000e0000` - `\U000e007f`) y overrides bidireccionales.
  - Recorte automático de firmas mecánicas (`-- \n`, `Enviado desde mi iPhone`, `Get Outlook for iOS`, etc.) y eliminación de cláusulas legales y disclaimers de privacidad (RGPD) para evitar gasto inútil de tokens y alucinaciones en modelos de lenguaje.
- **Nodo Autónomo `transform.clean_email` y Mejoras en `input.imap`**:
  - `input.imap` ahora soporta `clean_body`, `strip_signatures` y `max_body_chars` configurables, limpiando tanto texto plano como HTML y sanitizando remitente y asunto.
  - Nuevo nodo en catálogo `transform.clean_email` para limpiar correspondencia proveniente de webhooks, archivos o fuentes externas.
- **Enriquecimiento por Elementos (`as_field`) y Límite Manual de Contexto (`max_context`) en `transform.agent`**:
  - Cuando se ejecuta una LLM sobre listas (`per_item: true`), el parámetro `as_field` permite inyectar la respuesta del modelo en un campo determinado (ej. `resumen` o `categoria`) preservando intactos los metadatos originales del correo (`id`, `subject`, `from`, `date`, `attachments`).
  - Parámetro configurable `max_context`: permite fijar manualmente el tamaño de la ventana de contexto. Si la entrada más los prompts superan el 90% del presupuesto disponible tras reservar los tokens de salida, se compacta automáticamente con el algoritmo extractivo Luhn+ antes de emitir la petición HTTP, previniendo errores de desbordamiento en el servidor.
- **Plantilla Oficial "Resumen Diario de Correos" (`resumen-correos-diario`)**:
  - Flujo preconfigurado IMAP $\rightarrow$ LLM Asistente Ejecutivo (con system prompt defensivo que trata los correos como datos externos no confiables y formatea prioridades en Urgentes / Informativos / Boletines) $\rightarrow$ Telegram.

### M. Integración Nativa con Git y Auditoría Continua de Código (55 Nodos en Total)
Para transformar Dédalo en un orquestador de CI/CD ligero, GitOps y revisión de seguridad en servidores de bajo consumo:
- **Cero Dependencias en Python**: Implementado directamente sobre el ejecutable `git` del sistema operativo mediante `subprocess.run()`, sin utilizar librerías de alto consumo o fugas de memoria (`GitPython`, `pygit2`).
- **Nodo de Entrada `input.git`**:
  - `log`: Historial estructurado con autor, email, fecha, hash y archivos afectados, con soporte para ventanas temporales (`since="24 hours ago"`) y delimitadores ASCII que previenen colisiones de formato.
  - `diff`: Extracción del parche unificado entre revisiones o ramas con filtro de extensiones (`path_filter`) y límite de seguridad (`max_chars`, por defecto 8000 caracteres) para prevenir desbordamientos de contexto en LLMs.
  - `clone_or_pull`: Descarga inicial optimizada con clones shallow (`--depth 1 --no-tags`) que cuidan el almacenamiento de la tarjeta MicroSD, y actualización mediante `pull --ff-only`.
  - `status`: Inspección del árbol de trabajo para detectar archivos modificados o sin seguimiento.
- **Nodo de Salida `output.git`**:
  - Staging selectivo, generación de commits con mensaje y autor configurable (`author_name`, `author_email`) y push remoto opcional para usar Git como base de datos o almacenamiento versionado (Obsidian, notas, reportes).
- **Plantilla Oficial "Auditoría de Commits con IA" (`auditoria-commits-git`)**:
  - Conecta `input.git` (modo diff) $\rightarrow$ `transform.agent` (auditoría defensiva buscando credenciales expuestas, regresiones de seguridad y resumen de cambios) $\rightarrow$ `output.telegram`.

### N. Microarnés de Programación ReAct, Herramientas Estructuradas y Compactación Luhn+ (56 Nodos en Total)
Para permitir resolución autónoma de tareas de código, pruebas de regresión y refactorización guiada:
- **Protocolo de Tool Calling Nativo (OpenAI Compatible)**:
  - Implementado directamente mediante peticiones HTTP estándar con `httpx` (cero dependencias externas adicionales), conectando con servidores de inferencia locales (Ollama, vLLM) o remotos (OpenAI, OpenRouter, DeepSeek).
- **Paleta Canónica de 7 Herramientas y Presets**:
  - `ALL_TOOL_NAMES`: `("read", "bash", "edit", "write", "grep", "find", "ls")`.
  - `CODING_TOOL_NAMES`: `("read", "bash", "edit", "write")`.
  - `READ_ONLY_TOOL_NAMES`: `("read", "grep", "find", "ls")`.
  - Herramienta `read`: lectura con numeración de líneas y rangos acotados.
  - Herramienta `edit`: sustitución atómica verificando unicidad estricta del bloque `old_str`.
  - Herramienta `write`: escritura en disco creando subdirectorios automáticamente.
  - Herramienta `bash`: ejecución en el espacio de trabajo con timeout y captura de código de salida.
  - Herramientas `grep`, `find` y `ls`: exploración selectiva ignorando carpetas de dependencias y binarios (`.git`, `.venv`, etc.).
- **Enjaulado de Rutas (Path Sandboxing)**:
  - Confinamiento estricto al `workspace_dir` mediante `Path.resolve().is_relative_to(base_dir)`. Todo intento de lectura o modificación fuera del espacio de trabajo es bloqueado con error de seguridad.
- **Autodetección de Ventana de Contexto del Servidor**:
  - Función `_detect_context_window`: consulta `/api/show` en Ollama (extrayendo `num_ctx` o `model_info`), `/models` en endpoints OpenAI/OpenRouter y tabla de heurísticas para familias conocidas (`gpt-4o`, `claude`, `qwen2.5-coder`, `deepseek`, `llama-3.x`).
- **Compactación Automática con Luhn+ al 90% de Saturación**:
  - Evaluación continua del presupuesto de tokens (`~3.5` caracteres por token).
  - Al alcanzar el 90% del contexto, se preservan los prompts de inicio y las llamadas recientes, y se sintetiza el historial intermedio denso mediante el algoritmo extractivo `Luhn+` (filtrado de stopwords, normalización por longitud, sesgo de posición y diversidad MMR).
  - Truncado defensivo adicional sobre salidas de herramientas individuales desproporcionadas.
- **Plantillas Oficiales con Coder**:
  - **Triaje y propuesta de parche ante excepciones (`triaje-errores-coder`)**: Recibe trazas de excepción vía webhook, inspecciona el código circundante en modo de sólo lectura con `read`/`grep`, analiza la causa raíz del fallo y remite el diagnóstico con el parche propuesto a Telegram sin alterar archivos en disco.
  - **Generación automática de tests (`generar-tests-coder`)**: Extrae el diff de los últimos cambios de código con `input.git`, redacta nuevos tests unitarios en `tests/` con `write`/`edit` imitando la suite existente, ejecuta `pytest` mediante `bash` hasta validar que pasan, y genera un commit atómico con `output.git`.
  - **Auto-reparación de scrapers web (`auto-reparar-scraper`)**: Descarga el HTML actualizado con `input.http_call` (curl_cffi con impersonación de navegador), inspecciona selectores en `scrapers/extractor.py`, aplica parches con `edit` y valida la ejecución local mediante `bash` antes de alertar por Telegram.
  - **Sincronización de documentación con Git (`sincronizar-docs-codigo`)**: Extrae el diff de los últimos cambios de código con `input.git`, compara signaturas y argumentos modificados contra `README.md`, ajusta la documentación con `edit` y genera un commit atómico con `output.git`.

---

## 5. Estructura del Repositorio

```text
dedalo/
├── data/
│   ├── flows/              # Definiciones JSON y scripts Python compilados
│   ├── history.db          # Historial de ejecuciones en SQLite (modo WAL, cache 2MB)
│   ├── secrets.json        # Tokens y credenciales (permisos 0600)
│   └── state/              # Estados persistentes entre ejecuciones (p. ej. RSS)
├── dedalo/
│   ├── __init__.py
│   ├── app.py              # API REST con FastAPI (soporta flag DEDALO_HEADLESS)
│   ├── compiler.py         # Compilador de grafos visuales a scripts Python secuenciales
│   ├── cron.py             # Evaluador de cron nativo ultraligero (reemplazo de croniter)
│   ├── headless.py         # Ejecutor demonio puro sin servidor web (~14-16 MB RAM)
│   ├── history.py          # Gestor de base de datos SQLite para historial y purga
│   ├── llms.py             # Gestión multi-endpoint de LLMs y resolución con secretos
│   ├── runtime.py          # Ejecutor de nodos, sandbox y compact_memory()
│   ├── scheduler.py        # Planificador de tareas con cron nativo y compactación
│   ├── secrets.py          # Carga y almacenamiento seguro de secretos
│   ├── static/             # Frontend autónomo (HTML, CSS y JS sin empaquetar)
│   │   ├── dedalo.css
│   │   ├── dedalo.js
│   │   └── index.html
│   ├── store.py            # Persistencia de flujos en JSON/PY y empaquetado ZIP
│   └── templates.py        # Plantillas de inicio rápido
├── tests/                  # Pruebas automatizadas (pytest - 119 tests)
│   ├── test_api.py
│   ├── test_coder.py       # Pruebas de transform.coder, herramientas, sandboxing y Luhn+
│   ├── test_compiler.py
│   ├── test_cron.py        # Pruebas del evaluador cron e intervalos nativo
│   ├── test_debug.py       # Pruebas de ejecución aislada, paso a paso y preview en vivo
│   ├── test_email_digest.py # Pruebas de sanitización anti prompt-injection y digest
│   ├── test_git.py         # Pruebas de integración nativa con Git (log, diff, commit, push)
│   ├── test_history.py
│   ├── test_llms.py        # Pruebas de gestión multi-endpoint de LLMs y fallback
│   ├── test_runtime.py
│   ├── test_secrets.py
│   └── test_store.py       # Pruebas de exportación/importación ZIP y Zip-Slip
├── .dockerignore           # Exclusión estricta de secretos, caches y venv
├── Dockerfile              # Construcción multi-stage segura con usuario no-root
├── docker-compose.yml      # Despliegue con límites de memoria y endurecimiento
├── pyproject.toml          # Dependencias mínimas (sin croniter ni dateutil)
├── run.sh                  # Arranque con opciones --headless y --daemon
├── stop.sh                 # Script para detener el servicio
└── README.md               # Documentación general, Docker y systemd
```

---

## 6. Guía para Continuar el Desarrollo

### Entorno y Ejecución
Para trabajar en este proyecto de forma aislada:
```bash
cd dedalo

# Arrancar la aplicación (en http://localhost:8080)
./run.sh

# Ejecutar la suite de pruebas automatizadas
.venv/bin/pytest

# Detener el servidor
./stop.sh
```

### Ideas y Posibles Siguientes Pasos (Roadmap)
1. **Librería de Componentes / Subflujos**: Capacidad de reutilizar un flujo dentro de otro como un único nodo compuesto.
2. **Métricas en Vivo en Canvas**: Indicador numérico de elementos procesados o tiempo transcurrido superpuesto en cada conector de salida.
3. **Historial de Versiones Git Local**: Registro de revisiones locales automáticas para cada modificación de flujo guardada.
4. **Puntos de Interrupción Condicionales**: Pausar la ejecución en el lienzo solo cuando los datos cumplan una condición (ej. lista vacía o error).
