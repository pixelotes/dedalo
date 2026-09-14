# Exportación a Scripts Autónomos (Standalone) y Tree-Shaking

## 1. El Concepto de Exportación Standalone

Una de las capacidades más potentes y diferenciadoras de Dédalo es la posibilidad de diseñar y depurar visualmente un flujo complejo en el lienzo y, una vez validado, **exportarlo como un único fichero Python independiente** (`.py`) que:
- **No depende de Dédalo**: No necesitas tener el servidor de Dédalo instalado ni en ejecución.
- **No depende de paquetes pesados**: Se ejecuta con la librería estándar de Python más las mínimas dependencias externas indispensables que use ese flujo concreto (por ejemplo, `httpx`).
- **Es 100% legible y mantenible**: Puedes abrir el archivo generado, auditarlo, modificarlo con cualquier editor y versionarlo en tu repositorio Git.

---

## 2. Tree-Shaking Dinámico en el Compilador

Cuando se genera un script autónomo mediante la función `compiler.compile_standalone(graph)` o desde la interfaz web (botón de descarga/exportación):

1. **Inspección de Nodos Utilizados**:
   El compilador analiza los tipos de nodos presentes en el grafo.
2. **Inclusión Selectiva de Librerías (Tree-Shaking)**:
   A diferencia de un template estático que importaría todas las librerías posibles, Dédalo genera únicamente los bloques de código y las importaciones estrictamente necesarias:
   - Si el flujo no hace peticiones HTTP ni llamadas a LLMs $	o$ **no importa `httpx`**.
   - Si no utiliza `output.evidence_vault` $	o$ **no importa `gzip` ni `hashlib`**.
   - Si no interactúa con SQLite $	o$ **no importa `sqlite3`**.
   - Si no utiliza expresiones regulares $	o$ **no importa `re`**.
   - Si no utiliza agentes $	o$ **no incluye el microarnés ni la función `call_llm`**.

```text
┌───────────────────────────────────────────────┐
│              Grafo en el Lienzo               │
│  [input.rss] -> [transform.filter] -> [file]  │
└───────────────────────┬───────────────────────┘
                        │
                        ▼ compile_standalone()
┌───────────────────────────────────────────────┐
│          Script Python Autoportante           │
│  - Imports mínimos (xml, urllib, pathlib)     │
│  - Runtime empotrado específico (rss, filter) │
│  - Función run(ctx) + bloque __main__         │
└───────────────────────────────────────────────┘
```

---

## 3. Estructura del Script Exportado

El fichero generado contiene tres secciones bien delimitadas:

1. **Cabecera y Centinelas**:
   Define `SKIPPED`, la clase `NodeResult` y la clase `Inputs` para gestionar los datos que fluyen entre nodos de forma unificada.
2. **Funciones de Runtime Mínimas**:
   Copia únicamente las implementaciones de las funciones correspondientes a los nodos del grafo (por ejemplo, `rss`, `filter_items` y `file_output`).
3. **Función Principal `run()` y Bloque de Ejecución**:
   ```python
   def run(ctx=None):
       # Evaluación topológica determinista con sentinelas de omisión
       ...

   if __name__ == "__main__":
       results = run()
       print(results)
   ```

---

## 4. Métodos de Despliegue del Script Exportado

Una vez descargado el script (ej. `auditoria_k8s.py`), puedes ejecutarlo en cualquier máquina Linux, Mac o Windows sin necesidad de montar infraestructura adicional.

### 4.1. Ejecución Directa en Consola
```bash
python3 auditoria_k8s.py
```

### 4.2. Planificación con Cron del Sistema
Para automatizar la ejecución periódica sin servicios en segundo plano:
```cron
# Ejecutar cada hora en punto y volcar logs
0 * * * * /usr/bin/python3 /opt/scripts/auditoria_k8s.py >> /var/log/auditoria_k8s.log 2>&1
```

### 4.3. Despliegue con Temporizadores Systemd (Systemd Timers)
Ideal para distribuciones Linux modernas y servidores de producción:

1. Crear el servicio `/etc/systemd/system/auditoria-k8s.service`:
   ```ini
   [Unit]
   Description=Auditoria de Seguridad K8s (Script Standalone)
   After=network.target

   [Service]
   Type=oneshot
   User=appuser
   WorkingDirectory=/opt/scripts
   Environment="ATLASSIAN_TOKEN=tu_token_aqui"
   ExecStart=/usr/bin/python3 /opt/scripts/auditoria_k8s.py
   ```

2. Crear el temporizador `/etc/systemd/system/auditoria-k8s.timer`:
   ```ini
   [Unit]
   Description=Ejecutar Auditoria K8s cada 30 minutos

   [Timer]
   OnCalendar=*:0/30
   Persistent=true

   [Install]
   WantedBy=timers.target
   ```

3. Habilitar el temporizador:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now auditoria-k8s.timer
   ```

### 4.4. Micro-Contenedor Docker (Menos de 60 MB)
```dockerfile
FROM python:3.12-alpine
WORKDIR /app
RUN pip install --no-cache-dir httpx
COPY auditoria_k8s.py .
CMD ["python", "auditoria_k8s.py"]
```
