# Dependencias, Stack Tecnológico y Rendimiento

## 1. El Stack Tecnológico Mínimo

Dédalo está construido con una política estricta de **mínimas dependencias externas**. Mientras que proyectos similares arrastran cientos de paquetes transitivos, el núcleo de Dédalo se apoya casi en su totalidad en la rica **Librería Estándar de Python 3.11+**:

| Funcionalidad | Módulo Utilizado | Tipo |
| :--- | :--- | :--- |
| **Servidor Web y API REST** | `fastapi` / `starlette` + `uvicorn` | Externa (Única obligatoria) |
| **Peticiones HTTP y LLMs** | `httpx` | Externa |
| **Evasión Antibot y TLS Impersonation** | `curl_cffi` | Externa (Opcional) |
| **Bóveda de Evidencias Forenses** | `gzip`, `tarfile`, `hashlib` | Estándar de Python |
| **Auditoría e Historial de Ejecuciones** | `sqlite3` | Estándar de Python |
| **Ejecución Shell y Comandos** | `subprocess` | Estándar de Python |
| **Conexiones MQTT de Bajo Nivel** | `socket`, `ssl` | Estándar de Python |
| **Correo Electrónico** | `imaplib`, `smtplib`, `email` | Estándar de Python |
| **Manipulación de Fechas y Textos** | `datetime`, `html`, `re`, `json`, `base64` | Estándar de Python |
| **Frontend Web** | Vanilla JavaScript + SVG + CSS nativo | Cero dependencias |

---

## 2. Decisiones Técnicas: Lo que se Decidió NO Usar y Por Qué

### 2.1. Rechazo de Node.js y Ecosistema NPM
- **Motivo**: El runtime de V8 de Node.js consume por sí solo entre 80 y 120 MB de memoria en reposo antes de cargar la primera línea de código de una aplicación.
- **Ventaja en Dédalo**: Al no existir `package.json`, `node_modules` ni empaquetadores como Webpack o Vite, no hay builds lentos ni riesgo de vulnerabilidades en dependencias JavaScript de terceros.

### 2.2. Rechazo de Redis, RabbitMQ o Celery
- **Motivo**: Mantener un broker de mensajería y una cola distribuida es fundamental para sistemas que procesan decenas de miles de eventos por segundo, pero supone un coste inasumible para automatizaciones domésticas o servidores pequeños (60-150 MB extra de RAM).
- **Ventaja en Dédalo**: Dédalo utiliza un modelo de ejecución directa en hilo o subproceso ligero. La persistencia y el historial residen en SQLite (`data/history.db`), con escrituras directas en microsegundos y transacciones ACID nativas.

### 2.3. Rechazo de LangChain / LangGraph
- **Motivo**: LangChain introduce más de 40 dependencias transitivas (Pydantic, Tenacity, dataclasses-json, etc.), multiplicando los tiempos de arranque y provocando incompatibilidades entre versiones.
- **Ventaja en Dédalo**: Todas las capacidades agénticas (bucle ReAct de `transform.coder`, sanitización de secretos en `transform.analyzer`, modo JSON y streaming) están implementadas en funciones directas mediante peticiones HTTP estándar contra `/v1/chat/completions`.

---

## 3. Mediciones Reales de Rendimiento y Consumo de Memoria

Las siguientes mediciones han sido tomadas en una **Raspberry Pi 3B (1 GB RAM, Quad-Core ARM Cortex-A53 a 1.2 GHz)** ejecutando Raspberry Pi OS (Debian 64-bit):

### 3.1. Consumo de Memoria RAM (RSS)

```text
┌───────────────────────────────────────────────────────────────┐
│              Comparativa de Consumo de RAM (RSS)              │
├────────────────────────────────┬──────────────────────────────┤
│ Plataforma                     │ Memoria Consumida en Reposo  │
├────────────────────────────────┼──────────────────────────────┤
│ Apache Airflow (mínimo)        │ ~ 1.100 - 1.400 MB           │
│ n8n (Node.js + dependencias)   │ ~ 380 - 550 MB               │
│ Node-RED (Node.js)             │ ~ 160 - 240 MB               │
│ Dédalo (Modo Completo Web+API) │ ~ 26 - 32 MB                 │
│ Dédalo (Modo Demonio Cron)     │ ~ 14 - 17 MB                 │
│ Script Standalone Exportado    │ ~ 11 - 15 MB                 │
└────────────────────────────────┴──────────────────────────────┘
```

### 3.2. Tiempos de Arranque y Latencia
- **Tiempo de arranque del servidor**: Menos de **180 ms** desde el comando `./run.sh` hasta la aceptación de peticiones HTTP en el puerto 8080.
- **Carga de la interfaz web**: Menos de **40 ms** (el bundle completo de HTML, CSS y JS pesa menos de 95 KB sin comprimir).
- **Sobrecarga del compilador**: Menos de **2 ms** para compilar un grafo de 20 nodos a su script de Python plano correspondiente.
- **Uso de CPU en reposo**: **0.0%**, despertando únicamente cuando el planificador de cron alcanza el segundo programado o entra un webhook.
