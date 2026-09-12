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

Para consultar el estado técnico completo, las decisiones de arquitectura, la relación con Tomos y el registro de mejoras implementadas, consulta [ESTADO_DEL_PROYECTO.md](ESTADO_DEL_PROYECTO.md).

---

## Instalación rápida

```bash
git clone <tu-repo> dedalo
cd dedalo

# Arrancar (crea automáticamente el venv si no existe)
chmod +x run.sh stop.sh
./run.sh
```

Abre en tu navegador `http://localhost:8080`.

---

## Despliegue como servicio en Raspberry Pi (systemd)

Para que arranque automáticamente al encender tu Pi:

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
ExecStart=/home/pi/dedalo/.venv/bin/uvicorn dedalo.app:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5
EnvironmentFile=-/home/pi/dedalo/.env

[Install]
WantedBy=multi-user.target
```

3. Activa e inicia el servicio:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now dedalo
```

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
