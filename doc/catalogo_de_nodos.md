# Catálogo Completo de Nodos de Dédalo

Dédalo cuenta con **64 nodos modulares** categorizados en tres familias:
- **Entradas (`input.*`)**: 25 nodos para ingerir datos desde APIs, servicios, colas, hardware, repositorios o archivos.
- **Transformaciones (`transform.*`)**: 23 nodos para limpiar, filtrar, agregar, calcular, deduplicar o enriquecer datos con algoritmos locales o agentes LLM.
- **Salidas (`output.*`)**: 16 nodos para persistir evidencias, abrir incidencias, actualizar wikis, enviar alertas a mensajería o ejecutar acciones.

---

## 1. Nodos de Entrada (`input.*`)

### `input.telemetry`
- **Etiqueta**: Telemetría de Incidente.
- **Descripción**: Ingiere eventos brutos, trazas o logs de incidentes para procesos de SecOps y SRE. Admite JSON en línea o lectura de ficheros locales de logs.
- **Parámetros**: `incident_id` (opcional, autogenerado si está vacío), `resource` (clúster, pod, tenant), `telemetry_json`, `file_path`.
- **Salida**: Diccionario con `incident_id`, `resource`, `timestamp` y los datos de telemetría parseados.

### `input.jira_issues`
- **Etiqueta**: Jira Issues (JQL).
- **Descripción**: Realiza búsquedas avanzadas en Jira Cloud REST API v3 utilizando el lenguaje de consultas JQL.
- **Parámetros**: `instance_url` (*requerido*), `email`, `secret` (API Token), `jql`, `fields` (lista de campos separados por comas), `max_results`, `timeout`.
- **Salida**: Diccionario con la lista de `issues` enriquecidos (clave, resumen, estado, prioridad, descripción ADF o texto, asignado), total y consulta ejecutada.

### `input.confluence_pages`
- **Etiqueta**: Confluence Páginas (CQL).
- **Descripción**: Recupera páginas y documentación técnica desde Confluence Cloud mediante búsquedas por espacio/título o consultas CQL avanzadas.
- **Parámetros**: `instance_url` (*requerido*), `email`, `secret`, `space_key`, `title`, `cql`, `limit`, `timeout`.
- **Salida**: Diccionario con `pages` (lista de páginas con `id`, `title`, `space`, `version`, `content_storage_xhtml`, `content_text` y enlaces web).

### `input.git`
- **Etiqueta**: Lector Git.
- **Descripción**: Lee información de repositorios Git locales o remotos sin necesidad de instalar librerías pesadas (usa el binario `git`).
- **Acciones**: `log` (historial de commits), `diff` (parche de cambios con limitador de caracteres), `clone_or_pull` (clonado o actualización shallow), `status` (árbol de trabajo).
- **Parámetros**: `action`, `repo_path`, `url`, `revision` (ej. `HEAD~1..HEAD`), `limit`, `path_filter`, `max_chars`.

### `input.webhook`
- **Etiqueta**: Webhook Entrante.
- **Descripción**: Expone automáticamente un endpoint HTTP en el servidor de Dédalo (`/api/webhook/{nombre_del_flujo}`) para recibir payloads externos (alertas de Grafana, GitHub actions, Defender, etc.).
- **Salida**: Diccionario con el JSON recibido o texto del cuerpo HTTP.

### `input.http_call`
- **Etiqueta**: Traer de una API / Web.
- **Descripción**: Realiza peticiones HTTP (GET, POST, PUT, DELETE) con autenticación flexible y motor dual: `httpx` estándar o `curl_cffi` para suplantación de huellas TLS y evasión de antibots (Cloudflare).
- **Parámetros**: `url` (*requerido*), `method`, `headers`, `body`, `secret`, `auth_header`, `engine`, `impersonate` (chrome, safari, firefox).

### `input.web_read`
- **Etiqueta**: Extraer página web.
- **Descripción**: Descarga el contenido completo de una URL web y extrae el texto legible o HTML sanitizado.

### `input.web_search`
- **Etiqueta**: Búsqueda web.
- **Descripción**: Búsqueda en internet mediante DuckDuckGo (gratuito, sin credenciales), Tavily o Exa AI con filtrado de snippets y enlaces.

### `input.imap`
- **Etiqueta**: Lector Correo (IMAP).
- **Descripción**: Conecta a buzones de correo vía IMAP/TLS, busca mensajes no leídos o filtrados por asunto/remitente y extrae cuerpo, remitente y adjuntos con sanitización de inyecciones.

### `input.rss`
- **Etiqueta**: Feed RSS / Atom.
- **Descripción**: Lee canales de noticias y sindicación con memoria persistente de elementos ya procesados (`only_new: True`).

### `input.command`
- **Etiqueta**: Comando del Sistema (Shell).
- **Descripción**: Ejecuta un comando en la shell del servidor o contenedor local y captura la salida combinada estándar y de error.

### `input.csv_file` y `input.json_file`
- **Etiquetas**: Fichero CSV / Fichero JSON.
- **Descripción**: Lectura y parseo estructurado de archivos tabulares o documentos JSON desde el sistema de archivos local.

### `input.sqlite_query`
- **Etiqueta**: Consulta SQLite (SELECT).
- **Descripción**: Ejecuta consultas de lectura sobre bases de datos locales SQLite y devuelve filas como diccionarios.

### `input.pi_health`, `input.cpu_load`, `input.disk_free`
- **Etiquetas**: Métricas de Salud del Sistema.
- **Descripción**: Lee métricas de hardware de bajo nivel directamente desde `/proc` y `/sys` en Linux y Raspberry Pi (temperatura de CPU en °C, estrangulamiento térmico, memoria libre y porcentaje de disco) sin dependencias adicionales.

### `input.ssl_cert`, `input.dns_check`, `input.ping`
- **Etiquetas**: Monitores de Red y Seguridad.
- **Descripción**: Verificación de días restantes de certificados TLS, resolución y latencia DNS, y comprobaciones de conectividad ping (TCP o ICMP).

### `input.mqtt`
- **Etiqueta**: MQTT Suscribir.
- **Descripción**: Suscripción a topics en brokers MQTT locales o remotos implementada directamente sobre sockets nativos de Python.

### `input.weather` y `input.sitemap`
- **Etiquetas**: Meteorología / Lector de Sitemap XML.
- **Descripción**: Consultas climatológicas a Open-Meteo sin API key y rastreador de sitemaps XML para indexación web.

---

## 2. Nodos de Transformación (`transform.*`)

### `transform.analyzer`
- **Etiqueta**: Agente Analizador (SecOps / SRE).
- **Descripción**: Agente de diagnóstico especializado en ciberseguridad, respuesta a incidentes e infraestructura. Diagnostica causas raíz a partir de telemetría y evidencias.
- **Características clave**:
  - `sanitize_secrets: True`: Enmascara tokens, claves privadas, credenciales cloud y contraseñas antes de invocar al modelo.
  - `per_item: True`: Permite procesar lotes de elementos (ej. lista de tickets Jira o páginas Confluence) de forma secuencial enriqueciendo cada registro en `as_field`.
  - Soporta modo JSON estructurado.

### `transform.planner`
- **Etiqueta**: Agente Planificador (Remediación / Mitigación).
- **Descripción**: Toma el diagnóstico emitido por el analizador y elabora un plan estructurado de remediación con pasos de mitigación inmediata, solución definitiva y verificación. Genera salida en Markdown profesional o JSON estricto.

### `transform.coder`
- **Etiqueta**: Agente Programador (Coder).
- **Descripción**: Microarnés de programación iterativa con bucle ReAct multi-turno.
- **Herramientas disponibles**: `read`, `bash`, `edit`, `write`, `grep`, `find`, `ls`.
- **Compactación**: Si el contexto alcanza el 90% de la ventana del modelo, compacta el historial automáticamente mediante los algoritmos **Blackhole** o **Luhn+**.

### `transform.agent`
- **Etiqueta**: Agente LLM.
- **Descripción**: Invocación flexible a cualquier endpoint compatible con OpenAI API (Ollama local, vLLM, OpenAI, Groq, etc.) con inyección de variables `{entrada}`, control de temperatura y modo JSON.

### `transform.script`
- **Etiqueta**: Código Python.
- **Descripción**: Ejecución de código Python puro personalizado. Recibe el objeto `inp` con métodos como `.first_data()`, `.text()`, `.items()` y el contexto `ctx`. Permite cualquier transformación con latencia de microsegundos y cero consumo de tokens.

### `transform.filter` y `transform.array_ops`
- **Etiquetas**: Filtrar elementos / Operaciones de Lista.
- **Descripción**: Filtrado determinista sin IA (`contains`, `not_contains`, `regex`, `>`, `<`), proyecciones (*pluck* de campos), ordenación, límites numéricos y aplanado de listas.

### `transform.diff`
- **Etiqueta**: Detección de Cambios (Diff).
- **Descripción**: Conserva estado entre ejecuciones en `data/state/` utilizando hashes SHA-256. Emite únicamente los elementos verdaderamente nuevos o modificados, descartando los ya vistos.

### `transform.fuzzy_dedup`
- **Etiqueta**: Deduplicación Difusa.
- **Descripción**: Algoritmo de similitud de cadenas (Levenshtein y Jaro-Winkler) para agrupar y eliminar noticias o alertas casi idénticas sin llamar a modelos de lenguaje.

### `transform.summarize`
- **Etiqueta**: Resumir texto.
- **Descripción**: Resumen extractivo determinista mediante algoritmos clásicos de procesamiento del lenguaje: **Luhn**, **Luhn+** (con pesos posicionales) y **TextRank** basado en grafos.

### `transform.clean_email` y `transform.clean_html`
- **Etiquetas**: Limpiadores de Texto.
- **Descripción**: `clean_html` elimina etiquetas y scripts; `clean_email` neutraliza ataques de prompt injection ocultos en correos mediante CSS (`font-size:0`, `display:none`), elimina caracteres Unicode invisibles (zero-width) y recorta firmas habituales y cláusulas legales.

### `transform.threshold` y `transform.router`
- **Etiquetas**: Control de Flujo Condicional.
- **Descripción**: `threshold` detiene la ejecución emitiendo el centinela `SKIPPED` si un valor no supera los límites configurados; `router` bifurca la salida hacia diferentes ramas según reglas de condición.

### `transform.template`, `transform.table`, `transform.datetime`, `transform.aggregate`, `transform.keywords_rake`, `transform.delay`, `transform.merge`
- **Descripción**: Operaciones auxiliares para formateo de plantillas, tablas Markdown/HTML, cálculos temporales con zona horaria, agregados estadísticos (suma, media, percentiles), extracción de palabras clave con RAKE, retardos y combinación de múltiples ramas.

---

## 3. Nodos de Salida (`output.*`)

### `output.evidence_vault`
- **Etiqueta**: Custodia de Evidencia (Evidence Vault).
- **Descripción**: Almacén inmutable de custodia forense para incidentes de SecOps y SRE.
- **Funcionamiento**: Guarda la telemetría comprimida en `.tar.gz` o `.json.gz`, calcula el hash criptográfico SHA-256 del contenido y genera un manifiesto `.meta.json` inmutable para auditoría legal o técnica.

### `output.jira_issue`
- **Etiqueta**: Operaciones Jira (Crear / Comentar / Modificar).
- **Descripción**: Ejecuta acciones contra Jira Cloud REST API v3:
  - `create`: Abre una nueva incidencia con tipo de issue, prioridad y componentes.
  - `add_comment`: Añade comentarios con formato enriquecido Atlassian Document Format (ADF).
  - `update`: Modifica campos como prioridad, resumen o etiquetas.
  - `transition`: Cambia el estado del ticket en el workflow (ej. "En curso", "Resuelto", "Cerrado").
  - Soporta procesamiento en lote (`batch`) cuando recibe una lista de tickets.

### `output.confluence_page`
- **Etiqueta**: Publicar o Actualizar en Confluence.
- **Descripción**: Gestiona páginas en Confluence Cloud usando formato de almacenamiento XHTML.
- **Modos**:
  - `create_or_update`: Si la página existe (por `page_id` o `title`), consulta su versión previa y hace `PUT` incrementando la versión (`version + 1`). Si no existe, la crea.
  - `append`: Conserva todo el contenido actual de la página y añade los nuevos párrafos o secciones al final.

### `output.git`
- **Etiqueta**: Publicar en Git.
- **Descripción**: Realiza `git add`, `git commit` con autor y mensaje configurables y opcionalmente `git push` hacia la rama remota configurada.

### `output.telegram`, `output.discord`, `output.slack`
- **Etiquetas**: Mensajería y Alertas.
- **Descripción**: Notificaciones inmediatas a chats, grupos o canales con soporte para Markdown, embeds y formato de alerta.

### `output.ntfy`
- **Etiqueta**: Notificación Push (ntfy.sh).
- **Descripción**: Notificaciones push instantáneas a teléfonos móviles o navegadores sin necesidad de registrar cuentas ni pagar suscripciones, mediante el protocolo abierto ntfy.

### `output.sqlite_exec`
- **Etiqueta**: Ejecutar en SQLite (INSERT/UPDATE).
- **Descripción**: Inserta o actualiza registros de datos de forma persistente en bases de datos SQLite locales.

### `output.email_smtp`
- **Etiqueta**: Enviar Correo (SMTP).
- **Descripción**: Envío de correos electrónicos con autenticación STARTTLS o SSL directo, cuerpo HTML o texto plano y adjuntos.

### `output.file`
- **Etiqueta**: Guardar en fichero.
- **Descripción**: Escritura de ficheros en disco en modos sobrescribir, añadir (*append*) o numerar automáticamente si ya existe.

### `output.command`
- **Etiqueta**: Ejecutar Acción (Shell).
- **Descripción**: Ejecuta comandos o scripts en el sistema anfitrión como paso final del flujo (reiniciar servicios, ejecutar ansible, etc.).

### `output.healthcheck`
- **Etiqueta**: Ping de Monitorización (Heartbeat).
- **Descripción**: Envía pings de confirmación periódica a servicios de monitorización tipo Healthchecks.io o Uptime Kuma para alertar si un flujo cron deja de ejecutarse.
