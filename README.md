# Dédalo (Standalone)

> Flujos de trabajo visuales ultraligeros que compilan a código Python plano.

Dédalo es una alternativa minimalista y ultraligera a n8n o Node-RED pensada para funcionar en **Raspberry Pi** y servidores de bajo consumo:

- **Consumo de RAM**: ~25-35 MB en reposo (frente a 400+ MB de n8n).
- **Cero Node.js**: Backend puro en Python (FastAPI + Uvicorn).
- **Cero Bundlers**: Interfaz web interactiva en Vanilla JavaScript y SVG sin dependencias de compilación.
- **Compilación a Python plano**: En vez de un motor de ejecución reactivo con colas pesadas, Dédalo compila cada grafo visual directamente a un script Python secuencial (`def run(ctx):`) usando operadores ternarios y centinelas `SKIPPED`.
- **LLM desacoplado**: Compatible con cualquier endpoint estilo OpenAI (`/v1/chat/completions`), ya sea Ollama en red local, llama-server, vLLM, Groq o OpenAI.
- **Gestión de secretos**: Almacena tokens de APIs y credenciales de forma aislada en archivos con permisos estrictos (`0600`).
- **Planificador en segundo plano**: Ejecución periódica mediante cron (`0 8 * * *`) o intervalos (`@every 30m`).
- **Evasión Antibot y TLS con `curl_cffi`**: Suplantación de navegadores reales (Chrome, Safari, Firefox) y User-Agents configurables en peticiones web (`input.http_call`, `input.web_read`, `input.rss`) para consultar webs protegidas por Cloudflare o antibots sin incurrir en los cientos de megabytes de un navegador headless.
- **Búsqueda Web Multifuncional (`input.web_search`)**: Búsqueda en internet lista para usar con DuckDuckGo (gratuito, sin registros ni claves de API), y soporte para APIs avanzadas de búsqueda como Tavily y Exa / Metaphor mediante credenciales seguras. Formatos de salida en lista estructurada JSON o texto Markdown listo para resumir.
- **Potente Batería Determinista sin LLM (52 Nodos en Catálogo)**: Detección de novedades con memoria persistente (`transform.diff`), deduplicación difusa (`transform.fuzzy_dedup`), agregador estadístico (`transform.aggregate`), formateador de tablas (`transform.table`), extracción RAKE (`transform.keywords_rake`), monitorización SSL/DNS/Ping (`input.ssl_cert`, `input.dns_check`, `input.ping`), mensajería MQTT (`input.mqtt`, `output.mqtt`), lector de correo IMAP (`input.imap`), lector de sitemaps XML (`input.sitemap`), meteorología abierta sin API key (`input.weather`), operaciones temporales y fechas en español (`transform.datetime`), pausas de flujo con jitter anti-bloqueo (`transform.delay`) y pings heartbeat (`output.healthcheck`).

Para consultar el estado técnico completo, las decisiones de arquitectura, la relación con Tomos y el registro de mejoras implementadas, consulta [ESTADO_DEL_PROYECTO.md](ESTADO_DEL_PROYECTO.md).

---

## Instalación rápida

```bash
git clone <tu-repo> dedalo
cd dedalo

# Arrancar con interfaz web completa
chmod +x run.sh stop.sh
./run.sh

# O bien en modo Headless (solo Webhooks y API, sin interfaz web)
./run.sh --headless

# O bien como Demonio Puro (solo Scheduler en segundo plano, ~14-18 MB RAM)
./run.sh --daemon
```

Abre en tu navegador `http://localhost:8080` (en modo con interfaz).

---

## Despliegue con Docker y Docker Compose

Dédalo incluye un `Dockerfile` multi-stage endurecido para seguridad (usuario no-root, `cap_drop: ALL`, `no-new-privileges`, `tmpfs` en RAM y health check nativo sin curl):

```bash
# Iniciar en segundo plano
docker compose up -d

# Ver logs
docker compose logs -f

# Detener el servicio
docker compose down
```

> **Nota sobre monitorización**: Al ejecutarse en un contenedor aislado, los nodos de monitorización (`input.pi_health`) leerán el entorno del contenedor. Si deseas monitorizar la temperatura o disco del host físico, revisa las líneas comentadas de volúmenes en `docker-compose.yml`.

---

## Despliegue como servicio en Raspberry Pi (systemd)

Para que arranque automáticamente al encender tu Pi con control estricto de memoria:

1. Crea el fichero de servicio:
```bash
sudo nano /etc/systemd/system/dedalo.service
```

2. Añade el siguiente contenido (ajustando usuario y ruta):
```ini
[Unit]
Description=Dédalo Automation Service
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

# Control de consumo de memoria con cgroups
MemoryHigh=65M
MemoryMax=85M

[Install]
WantedBy=multi-user.target
```

3. Activa e inicia el servicio:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now dedalo
```

> **Consejo para Raspberry Pi**: Instala `zram-tools` (`sudo apt install -y zram-tools`) para disponer de swap comprimido en RAM sin degradar la tarjeta MicroSD.

---

## Estructura de archivos de un flujo

Cada flujo guardado reside en `data/flows/`:
- `<slug>.json`: El documento canónico del grafo visual.
- `<slug>.py`: El script ejecutable generado automáticamente.
- `<slug>.meta.json`: Configuración de periodicidad cron y estado activo/pausado.
- `data/state/<slug>_rss.json`: Estado persistente entre ejecuciones (memoria de noticias ya vistas).

---

## Backporting a Tomos

El esquema de serialización JSON y la generación de código con operadores ternarios y el centinela `SKIPPED` son 100% compatibles con Tomos. Cualquier flujo creado en Dédalo Standalone puede copiarse a `data/skills/` en Tomos.
