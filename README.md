# Dédalo

Orquestador visual de flujos de trabajo diseñado para ejecutarse de forma continua en hardware de recursos reducidos, con el objetivo de funcionar en una Raspberry Pi 3B consumiendo entre 25 y 35 MB de memoria RAM.

A diferencia de alternativas como n8n o Node-RED, Dédalo no requiere Node.js, no utiliza empaquetadores en el frontend ni mantiene un motor de evaluación reactivo con colas pesadas en memoria. Cada grafo visual se compila directamente a un script de Python plano (`def run(ctx):`) basado en operadores ternarios y centinelas de omisión (`SKIPPED`).

El sistema incluye una amplia batería de nodos de transformación deterministas con el objetivo de ofrecer la máxima flexibilidad en el tratamiento de datos y evitar la dependencia de modelos de lenguaje (LLM) a menos que sea estrictamente necesario.

---

## Características técnicas

- **Bajo consumo**: 14 a 18 MB de RAM en modo demonio puro (planificador cron sin servidor web), 25 a 35 MB con servidor API y entorno visual.
- **Compilación directa a Python**: Los flujos visuales se traducen a código Python determinista en `data/flows/<nombre>.py`.
- **Frontend ligero sin dependencias**: Interfaz web en JavaScript vainilla y SVG nativo, con lienzo infinito y sin librerías de componentes pesadas.
- **Evaluador de cron integrado**: Parser nativo de expresiones cron e intervalos (`@every 30m`, `@hourly`) sin dependencias externas.
- **Compatibilidad web sin navegador headless**: Cliente HTTP dual con `curl_cffi` (huella TLS de navegador real, para sitios que rechazan clientes HTTP genéricos) y fallback a `httpx`.
- **Depuración paso a paso y previsualización en vivo**: Control de ejecución nodo a nodo en el lienzo, visor modal de entradas y salidas completas sin truncar y recálculo en tiempo real de transformaciones (regex, selectores CSS, plantillas) conforme se editan los parámetros.
- **Gestión local de secretos**: Almacenamiento aislado de credenciales en ficheros con permisos restringidos (`0600`) o mediante variables de entorno.

---

## Nodos principales

El catálogo incluye más de 50 nodos modulares (56 en total). Entre los más utilizados destacan:

### Nodos de entrada
- `input.git`: Lectura de repositorios Git locales o remotos (historial de commits, diff de cambios entre revisiones con filtro de rutas, clonado ligero shallow o estado del árbol).
- `input.rss`: Lector de canales RSS y Atom con memoria persistente de elementos ya procesados.
- `input.http_call` / `input.web_read`: Peticiones HTTP con suplantación de huella de navegador y extracción de contenido web.
- `input.imap`: Lector de correo electrónico vía IMAP con soporte TLS y sanitización automática de inyecciones y firmas.
- `input.web_search`: Búsqueda en internet mediante DuckDuckGo (sin API key), Tavily o Exa.
- `input.webhook`: Exposición del flujo como endpoint HTTP para recepción de eventos externos (sin autenticación propia: ver [Seguridad](#seguridad)).
- `input.mqtt`: Recepción de mensajes desde brokers MQTT locales (Mosquitto, Home Assistant) implementada en sockets nativos.
- `input.weather`: Consulta meteorológica en tiempo real a Open-Meteo con geocodificación automática.
- `input.pi_health`: Medición de temperatura, memoria, estrangulamiento térmico y carga en Raspberry Pi y sistemas Linux.
- `input.ssl_cert` / `input.dns_check` / `input.ping`: Comprobación de caducidad de certificados TLS, resolución DNS y latencia de red.

### Nodos de transformación
- `transform.coder`: Microarnés de programación iterativa con bucle ReAct multi-turno y herramientas estructuradas (`read`, `bash`, `edit`, `write`, `grep`, `find`, `ls`). Autodetecta la ventana de contexto del servidor de inferencia y compacta el historial previo con Luhn+ al alcanzar el 90% de saturación.
- `transform.html_extract`: Extracción de campos y registros mediante selectores CSS sobre documentos HTML.
- `transform.regex`: Evaluación de expresiones regulares con modos de coincidencia, extracción de grupos y sustitución.
- `transform.filter_items` / `transform.array_ops`: Filtrado condicional, ordenación, límites y proyección sobre listas de datos.
- `transform.diff`: Detección de novedades mediante almacenamiento persistente de firmas hash entre ejecuciones sucesivas.
- `transform.fuzzy_dedup`: Deduplicación de textos y registros basada en similitud de cadenas sin recurrir a IA.
- `transform.clean_email`: Sanitización de correos que neutraliza texto oculto por CSS (`display:none`, `font-size:0`, `color:transparent`), comentarios HTML, caracteres Unicode invisibles (zero-width) y recorta firmas habituales y cláusulas legales.
- `transform.datetime`: Cálculos de fechas y horas, desplazamientos relativos, cambio de zona horaria y formateo en lenguaje natural.
- `transform.summarize`: Resumen extractivo algorítmico mediante Luhn, Luhn+ y TextRank.
- `transform.script`: Ejecución de lógica personalizada en Python con aislamiento de variables.
- `transform.agent`: Invocación opcional a endpoints compatibles con la API de OpenAI (modelos locales como Ollama o servicios remotos), con soporte para modo JSON y enriquecimiento de campos (`as_field`) sobre listas.

### Nodos de salida
- `output.git`: Creación de commits y ejecución de push opcional con autoría y mensaje configurables hacia repositorios remotos.
- `output.telegram` / `output.discord`: Envío de mensajes y alertas formateadas a canales y chats privados.
- `output.sqlite`: Inserción y actualización directa de registros en bases de datos SQLite locales.
- `output.file`: Escritura y concatenación de ficheros en disco (Markdown, JSON, texto plano).
- `output.mqtt`: Publicación de telemetría o comandos hacia brokers MQTT.
- `output.healthcheck`: Envío de pings de monitorización a servicios como Healthchecks.io o Uptime Kuma.

---

## Casos de uso representativos

1. **Monitorización selectiva de fuentes de noticias o boletines oficiales**:
   Descarga de canales RSS o artículos web, descarte de elementos duplicados mediante `transform.fuzzy_dedup` o `transform.diff`, filtrado de temáticas relevantes y envío de resumen a un canal de Telegram.

2. **Briefing diario de correo electrónico sin riesgo de inyección**:
   Lectura de correos no leídos con `input.imap`, depuración de texto oculto y firmas con `transform.clean_email`, compilación de un resumen ejecutivo priorizado mediante `transform.agent` y envío consolidado al móvil a primera hora de la mañana.

3. **Vigilancia de infraestructura y hardware en Raspberry Pi**:
   Supervisión continua de temperatura de CPU, porcentaje de disco libre y validez de certificados TLS. Un nodo `transform.threshold` detiene el flujo si los valores son normales y dispara alertas críticas únicamente si se sobrepasan los límites.

4. **Procesamiento y archivo estructurado de facturas o notificaciones**:
   Recepción de eventos vía webhook o IMAP, extracción de datos clave mediante expresiones regulares o agente en modo JSON, e inserción automática en una base de datos SQLite local para consulta posterior.

5. **Auditoría continua de commits y seguridad en Git**:
   Extracción automática del parche de cambios reciente mediante `input.git` (modo diff con límite máximo de caracteres para proteger el contexto), análisis de vulnerabilidades y credenciales con `transform.agent` y envío de alertas preventivas a Telegram ante fallos críticos.

6. **Corrección y refactorización asistida de código en bucle cerrado**:
   Activación de `transform.coder` para resolver una incidencia o ajustar un archivo fuente en disco local. El agente consulta archivos con `read`/`grep`, prueba cambios con `bash` y edita de forma atómica con `edit`, compactando memoria vía Luhn+ si el contexto se satura, antes de registrar el resultado.

---

## Requisitos e instalación

Se requiere Python 3.11 o superior en un sistema operativo tipo Unix (Linux, Raspberry Pi OS, macOS).

```bash
git clone https://github.com/pixelotes/dedalo.git
cd dedalo

# Instalar entorno y dependencias
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Iniciar la aplicación con interfaz web
./run.sh
```

La aplicación estará disponible en `http://localhost:8080`.

> Dédalo no tiene autenticación. Antes de exponerlo fuera de `localhost`, lee la sección [Seguridad](#seguridad).

### Modos de ejecución

- **Completo (Web + API + Scheduler)**:
  ```bash
  ./run.sh
  ```

- **Headless (API REST y Webhooks, sin servir estáticos de UI)**:
  ```bash
  ./run.sh --headless
  ```

- **Demonio puro (Únicamente el planificador cron, consumo mínimo de RAM)**:
  ```bash
  ./run.sh --daemon
  ```

- **Detención**:
  ```bash
  ./stop.sh
  ```

---

## Despliegue con Docker

El proyecto incluye configuración multi-stage con restricciones de permisos (usuario no root, todas las capacidades de Linux revocadas y `no-new-privileges`):

```bash
docker compose up -d
docker compose logs -f
```

---

## Servicio systemd para Raspberry Pi

Para asegurar el arranque automático del servicio tras un reinicio y limitar el consumo de memoria:

1. Crear el fichero `/etc/systemd/system/dedalo.service`:

```ini
[Unit]
Description=Dedalo Automation Service
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/dedalo
Environment="MALLOC_TRIM_THRESHOLD_=65536"
Environment="PYTHONOPTIMIZE=1"
EnvironmentFile=-/home/pi/dedalo/.env
ExecStart=/home/pi/dedalo/.venv/bin/uvicorn dedalo.app:app --host 127.0.0.1 --port 8080 --no-access-log --limit-concurrency 15 --timeout-keep-alive 5
Restart=always
RestartSec=5

# Limites de memoria mediante cgroups
MemoryHigh=65M
MemoryMax=85M

[Install]
WantedBy=multi-user.target
```

2. Activar e iniciar:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now dedalo
```

---

## Seguridad

Dédalo ejecuta código: los flujos se compilan a Python y nodos como `transform.script`, `transform.coder` (con `bash`) y `output.git` actúan sobre el sistema con los permisos del usuario que lo ejecuta. Trátalo como una consola de administración.

- **Sin autenticación**: la interfaz web y la API REST (incluidas `/api/run`, `/api/flow` y `/api/secrets`) no piden credenciales. Quien llegue al puerto puede ejecutar código y leer o escribir secretos.
- **Escucha local**: usa `DEDALO_HOST=127.0.0.1` y accede por túnel SSH, Tailscale o un proxy inverso con autenticación (por ejemplo Caddy o Nginx con OIDC o basic auth). No lo publiques directamente en internet ni lo abras a toda la red local.
- **Sin CORS y comprobación de host**: la API no envía cabeceras CORS (la interfaz se sirve desde el mismo origen). Se rechazan las peticiones cuyo `Host` no esté en `DEDALO_ALLOWED_HOSTS` (por defecto `localhost,127.0.0.1,[::1]`; admite `*.dominio`) y las peticiones que modifican estado con un `Origin` distinto del `Host`. Si accedes por un dominio, IP de Tailscale o proxy inverso, añádelo a esa variable; el proxy debe conservar la cabecera `Host`. Las llamadas sin `Origin` (curl, webhooks de servidores) no se ven afectadas, pero sí deben usar un `Host` permitido.
- **Docker**: publica el puerto solo en local (`"127.0.0.1:8080:8080"`). Dentro del contenedor debe seguir escuchando en `0.0.0.0`.
- **Webhooks**: `/api/webhook/<flujo>` no valida el origen. Si necesitas recibir eventos de internet, expón solo esa ruta a través del proxy y protégela con un token o una lista de IP.
- **Comandos de shell** (`input.command`, `output.command` y el `bash` de `transform.coder`): los subprocesos no heredan `DEDALO_SECRET_*`, `DEDALO_MASTER_KEY*`, `DEDALO_LLM_KEY` ni `DEDALO_TELEGRAM_*` (añade más patrones con `DEDALO_ENV_DENY`). Además pueden aislarse con [bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`, sin root): solo ven `/usr`, `/bin`, `/lib*` y `/etc` en solo lectura, un `/tmp` vacío y su directorio de trabajo (`cwd`); `data/` queda oculto. `DEDALO_SANDBOX=off|auto|on`: por defecto `off` en los nodos de comando (no cambia el comportamiento de tus flujos) y `auto` en `transform.coder`; `auto` usa `bwrap` si funciona y si no ejecuta sin aislar, `on` falla si no está disponible. Cada nodo tiene la opción `sandbox` (puede pedir `on`, pero no desactivar un `on` global). Extras: `DEDALO_SANDBOX_RO` y `DEDALO_SANDBOX_RW` (rutas separadas por `:`) y `DEDALO_SANDBOX_NET=deny` para cortar la red. Con el sandbox activo un comando sin `cwd` arranca en un `/tmp` vacío, y `systemctl` y similares no llegan al host. La imagen Docker incluye `bwrap` (`--build-arg WITH_BWRAP=0` para omitirlo), pero el perfil por defecto de Docker bloquea los user namespaces: para usarlo dentro del contenedor, consulta `docker-compose.yml`. Fuera de Docker (Raspberry Pi OS, Debian): `apt install bubblewrap`.
- **`transform.script`**: el aislamiento es de variables, no un sandbox. No ejecutes flujos importados de terceros sin revisarlos.
- **Secretos**: se guardan cifrados (Fernet) en `data/secrets/` con permisos `0600`; la `llm_key` de `data/config.json` también. La clave maestra sale de `DEDALO_MASTER_KEY` (o `DEDALO_MASTER_KEY_FILE`); si no defines ninguna se genera `data/master.key`, que protege frente a copias parciales pero no frente a quien lea todo `data/`. Para protección real, define la clave fuera de `data/` (variable de entorno, Docker secret). Genérala con `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` y guárdala aparte: sin ella los secretos no se pueden recuperar. Los secretos en claro de versiones anteriores se leen igual y se cifran solos al arrancar. Las variables `DEDALO_SECRET_<NOMBRE>` siguen funcionando y no se cifran (viven en el entorno).

Para reportar una vulnerabilidad, consulta [SECURITY.md](SECURITY.md).

---

## Pruebas automatizadas y calidad

Para ejecutar la suite completa de pruebas unitarias y el linter:

```bash
pytest -v
ruff check .
```

---

## Contribuir

Consulta [CONTRIBUTING.md](CONTRIBUTING.md) y el [CHANGELOG](CHANGELOG.md).

---

## Licencia

[Elastic License 2.0](LICENSE). Ver también [NOTICE](NOTICE).
