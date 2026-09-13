# Dedalo

Orquestador visual de flujos de trabajo disenado para ejecutarse de forma continua en hardware de recursos reducidos, con el objetivo de funcionar en una Raspberry Pi 3B consumiendo entre 25 y 35 MB de memoria RAM.

A diferencia de alternativas como n8n o Node-RED, Dedalo no requiere Node.js, no utiliza empaquetadores en el frontend ni mantiene un motor de evaluacion reactivo con colas pesadas en memoria. Cada grafo visual se compila directamente a un script de Python plano (`def run(ctx):`) basado en operadores ternarios y centinelas de omision (`SKIPPED`).

El sistema incluye una amplia bateria de nodos de transformacion deterministicos con el objetivo de ofrecer la maxima flexibilidad en el tratamiento de datos y evitar la dependencia de modelos de lenguaje (LLM) a menos que sea estrictamente necesario.

---

## Caracteristicas tecnicas

- **Bajo consumo**: 14 a 18 MB de RAM en modo demonio puro (planificador cron sin servidor web), 25 a 35 MB con servidor API y entorno visual.
- **Compilacion directa a Python**: Los flujos visuales se traducen a codigo Python determinista en `data/flows/<nombre>.py`.
- **Frontend ligero sin dependencias**: Interfaz web en JavaScript vainilla y SVG nativo, con lienzo infinito y sin librerias de componentes pesadas.
- **Evaluador de cron integrado**: Parser nativo de expresiones cron e intervalos (`@every 30m`, `@hourly`) sin dependencias externas.
- **Evasion antibot sin navegador headless**: Cliente HTTP dual con soporte para `curl_cffi` (suplantacion de huellas TLS y navegadores reales) y fallback a `httpx`.
- **Depuracion paso a paso y previsualizacion en vivo**: Control de ejecucion nodo a nodo en el lienzo, visor modal de entradas y salidas completas sin truncar y recalculo en tiempo real de transformaciones (regex, selectores CSS, plantillas) conforme se editan los parametros.
- **Gestion local de secretos**: Almacenamiento aislado de credenciales en ficheros con permisos restringidos (`0600`) o mediante variables de entorno.

---

## Nodos principales

El catalogo incluye mas de 50 nodos modulares. Entre los mas utilizados destacan:

### Nodos de entrada
- `input.rss`: Lector de canales RSS y Atom con memoria persistente de elementos ya procesados.
- `input.http_call` / `input.web_read`: Peticiones HTTP con suplantacion de huella de navegador y extraccion de contenido web.
- `input.imap`: Lector de correo electronico via IMAP con soporte TLS y sanitizacion automatica de inyecciones y firmas.
- `input.web_search`: Busqueda en internet mediante DuckDuckGo (sin API key), Tavily o Exa.
- `input.webhook`: Exposicion del flujo como endpoint HTTP publico para recepcion de eventos externos.
- `input.mqtt`: Recepcion de mensajes desde brokers MQTT locales (Mosquitto, Home Assistant) implementada en sockets nativos.
- `input.weather`: Consulta meteorologica en tiempo real a Open-Meteo con geocodificacion automatica.
- `input.pi_health`: Medicion de temperatura, memoria, estrangulamiento termico y carga en Raspberry Pi y sistemas Linux.
- `input.ssl_cert` / `input.dns_check` / `input.ping`: Comprobacion de caducidad de certificados TLS, resolucion DNS y latencia de red.

### Nodos de transformacion
- `transform.html_extract`: Extraccion de campos y registros mediante selectores CSS sobre documentos HTML.
- `transform.regex`: Evaluacion de expresiones regulares con modos de coincidencia, extraccion de grupos y sustitucion.
- `transform.filter_items` / `transform.array_ops`: Filtrado condicional, ordenacion, limites y proyeccion sobre listas de datos.
- `transform.diff`: Deteccion de novedades mediante almacenamiento persistente de firmas hash entre ejecuciones sucesivas.
- `transform.fuzzy_dedup`: Deduplicacion de textos y registros basada en similitud de cadenas sin recurrir a IA.
- `transform.clean_email`: Sanitizacion de correos que neutraliza texto oculto por CSS (`display:none`, `font-size:0`, `color:transparent`), comentarios HTML, caracteres Unicode invisibles (zero-width) y recorta firmas habituales y clausulas legales.
- `transform.datetime`: Calculos de fechas y horas, desplazamientos relativos, cambio de zona horaria y formateo en lenguaje natural.
- `transform.summarize`: Resumen extractivo algoritmico mediante Luhn, Luhn+ y TextRank.
- `transform.script`: Ejecucion de logica personalizada en Python con aislamiento de variables.
- `transform.agent`: Invocacion opcional a endpoints compatibles con la API de OpenAI (modelos locales como Ollama o servicios remotos), con soporte para modo JSON y enriquecimiento de campos (`as_field`) sobre listas.

### Nodos de salida
- `output.telegram` / `output.discord`: Envio de mensajes y alertas formateadas a canales y chats privados.
- `output.sqlite`: Insercion y actualizacion directa de registros en bases de datos SQLite locales.
- `output.file`: Escritura y concatenacion de ficheros en disco (Markdown, JSON, texto plano).
- `output.mqtt`: Publicacion de telemetria o comandos hacia brokers MQTT.
- `output.healthcheck`: Envio de pings de monitorizacion a servicios como Healthchecks.io o Uptime Kuma.

---

## Casos de uso habituales

1. **Monitorizacion de novedades web sin bloqueos antibot**:
   Extraccion periodica de ofertas o articulos mediante `input.http_call` con suplantacion de TLS, filtrado con `transform.html_extract` y `transform.diff`, y aviso inmediato a Telegram solo cuando aparezcan elementos nuevos.

2. **Briefing diario de correo electronico sin riesgo de inyeccion**:
   Lectura de correos no leidos con `input.imap`, depuracion de texto oculto y firmas con `transform.clean_email`, compilacion de un resumen ejecutivo priorizado mediante `transform.agent` y envio consolidado al movil a primera hora de la manana.

3. **Vigilancia de infraestructura y hardware en Raspberry Pi**:
   Supervision continua de temperatura de CPU, porcentaje de disco libre y validez de certificados TLS. Un nodo `transform.threshold` detiene el flujo si los valores son normales y dispara alertas criticas unicamente si se sobrepasan los limites.

4. **Procesamiento y archivo estructurado de facturas o notificaciones**:
   Recepcion de eventos via webhook o IMAP, extraccion de datos clave mediante expresiones regulares o agente en modo JSON, e insercion automatica en una base de datos SQLite local para consulta posterior.

---

## Requisitos e instalacion

Se requiere Python 3.11 o superior en un sistema operativo tipo Unix (Linux, Raspberry Pi OS, macOS).

```bash
git clone https://github.com/<tu-usuario>/dedalo.git
cd dedalo

# Instalar entorno y dependencias
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Iniciar la aplicacion con interfaz web
./run.sh
```

La aplicacion estara disponible en `http://localhost:8080`.

### Modos de ejecucion

- **Completo (Web + API + Scheduler)**:
  ```bash
  ./run.sh
  ```

- **Headless (API REST y Webhooks, sin servir estaticos de UI)**:
  ```bash
  ./run.sh --headless
  ```

- **Demonio puro (Unicamente el planificador cron, consumo minimo de RAM)**:
  ```bash
  ./run.sh --daemon
  ```

- **Detencion**:
  ```bash
  ./stop.sh
  ```

---

## Despliegue con Docker

El proyecto incluye configuracion multi-stage con restricciones de permisos (usuario no-root, capacidades revocadas y lectura exclusiva):

```bash
docker compose up -d
docker compose logs -f
```

---

## Servicio systemd para Raspberry Pi

Para asegurar el arranque automatico del servicio tras un reinicio y limitar el consumo de memoria:

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
ExecStart=/home/pi/dedalo/.venv/bin/uvicorn dedalo.app:app --host 0.0.0.0 --port 8080 --no-access-log --limit-concurrency 15 --timeout-keep-alive 5
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

## Pruebas automatizadas y calidad

Para ejecutar la suite completa de pruebas unitarias y el linter:

```bash
pytest -v
ruff check .
```
