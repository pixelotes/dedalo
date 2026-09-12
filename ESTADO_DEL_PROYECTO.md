# Dédalo Standalone — Estado del Proyecto, Arquitectura y Relación con Tomos

Este documento resume el estado técnico actual de **Dédalo Standalone**, su origen y sincronización con el proyecto **Tomos**, las decisiones de arquitectura adoptadas y el registro completo de mejoras implementadas. Sirve como referencia base para continuar el desarrollo de Dédalo como proyecto 100% autónomo.

---

## 1. Visión y Estado Actual

**Dédalo** es un orquestador visual de flujos de trabajo ultraligero diseñado específicamente para entornos de recursos limitados (como una Raspberry Pi o pequeños servidores domésticos) sin incurrir en la sobrecarga de herramientas como n8n, Node-RED o Airflow.

### Características Principales en Standalone:
- **Consumo mínimo de RAM**: ~25-35 MB en reposo (frente a los más de 400 MB habituales en aplicaciones basadas en Node.js o Electron).
- **Backend**: Python 3.10+ puro con FastAPI, Uvicorn y SQLite en modo WAL.
- **Frontend**: Vanilla JavaScript (ES6+) y SVG nativo. Sin empaquetadores (Webpack/Vite), sin frameworks pesados (React/Vue).
- **Compilación a Python plano**: Los grafos visuales no se interpretan paso a paso en un bucle reactivo; se compilan a un script Python determinista (`<slug>.py`) con operadores ternarios y el centinela `SKIPPED`.
- **Lienzo Infinito**: Sistema de transformación matricial con paneo por arrastre, zoom por rueda o controles en pantalla y coordenadas de mundo desacopladas.
- **Conexiones en bordes**: Puertos de entrada en el borde lateral izquierdo y bifurcaciones de éxito/fallo (`ok` / `error`) en el borde lateral derecho.
- **Historial de ejecuciones**: Base de datos SQLite integrada con rotación automática periódica cada 24 horas (elimina ejecuciones con más de 30 días).
- **Control de cambios**: Detección de modificaciones no guardadas con modal de confirmación antes de cambiar de flujo.
- **Suite de pruebas**: 29 pruebas automatizadas pasando al 100% en `pytest`.

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
- **Gestión de Cambios sin Guardar**:
  - Indicador de estado modificado (`isDirty`).
  - Diálogo modal de confirmación antes de cambiar a otro flujo o crear uno nuevo si hay cambios pendientes.
- **Desvinculación y Selección Contextual**:
  - Al hacer clic en un nodo se abren sus ajustes; al hacer clic en el fondo vacío se limpian las selecciones.
  - Desconexión de cables mediante clic derecho con eliminación instantánea del enlace.

### B. Historial de Ejecuciones
- Pestaña dedicada en la barra lateral con listado histórico de ejecuciones, fecha/hora, duración y estado (`OK`, `ERR`, `RUN`).
- Hoja modal de detalle para inspeccionar las entradas y salidas de cada nodo individual.
- Resaltado visual en el propio lienzo del resultado de la última prueba (`node-run-ok`, `node-run-err`, `node-run-skip`).
- Purga automática cada 24h para descartar ejecuciones de más de 30 días.

### C. Catálogo de Nodos Ampliado (14 Nuevos Tipos Ultraligeros)
Sin añadir dependencias pesadas y aprovechando la librería estándar de Python:
- **Entradas**: `input.webhook` (disparador HTTP), `input.sqlite_query` (consultas SQL locales), `input.command` (ejecución shell controlada), `input.pi_health` (telemetría de CPU/RAM de Raspberry Pi), `input.directory_scan` (escaneo de ficheros), `input.http_health` (comprobación de latencia/estado web).
- **Transformaciones**: `transform.template` (sustitución de plantillas con variables), `transform.array_ops` (ordenar, filtrar, recortar listas), `transform.regex` (extracciones y reemplazos con expresiones regulares), `transform.router` (bifurcador condicional a puerto `ok` o `error`).
- **Salidas**: `output.sqlite_exec` (escritura en base de datos local), `output.email_smtp` (envío de correos vía SMTP), `output.ntfy` (notificaciones push a móviles vía ntfy.sh), `output.discord` (webhooks con mensajes y embeds), `output.slack` (notificaciones para equipos), `output.command` (ejecución de comandos de sistema de salida).

---

## 5. Estructura del Repositorio

```text
dedalo/
├── data/
│   ├── flows/              # Definiciones JSON y scripts Python compilados
│   ├── history.db          # Historial de ejecuciones en SQLite (modo WAL)
│   ├── secrets.json        # Tokens y credenciales (permisos 0600)
│   └── state/              # Estados persistentes entre ejecuciones (p. ej. RSS)
├── dedalo/
│   ├── __init__.py
│   ├── app.py              # API REST con FastAPI (rutas de flujos, ejecución, webhooks)
│   ├── compiler.py         # Compilador de grafos visuales a scripts Python secuenciales
│   ├── history.py          # Gestor de base de datos SQLite para historial y purga
│   ├── runtime.py          # Ejecutor de nodos y funciones de contexto
│   ├── scheduler.py        # Planificador de tareas en segundo plano (cron/intervalos)
│   ├── secrets.py          # Carga y almacenamiento seguro de secretos
│   ├── static/             # Frontend autónomo (HTML, CSS y JS sin empaquetar)
│   │   ├── dedalo.css
│   │   ├── dedalo.js
│   │   └── index.html
│   ├── store.py            # Persistencia de flujos en ficheros JSON/PY
│   └── templates.py        # Plantillas de inicio rápido
├── tests/                  # Pruebas automatizadas (pytest)
│   ├── test_api.py
│   ├── test_compiler.py
│   ├── test_history.py
│   ├── test_runtime.py
│   └── test_secrets.py
├── pyproject.toml          # Configuración del paquete y dependencias mínimas
├── run.sh                  # Script de arranque rápido (crea venv si es necesario)
├── stop.sh                 # Script para detener el servicio
└── README.md               # Documentación general y despliegue systemd
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
2. **Depuración Paso a Paso**: Posibilidad de ejecutar un flujo hasta un nodo específico e inspeccionar el estado intermedio en vivo.
3. **Métricas en Vivo en Canvas**: Indicador numérico de elementos procesados o tiempo transcurrido superpuesto en cada conector de salida.
4. **Exportación / Importación ZIP**: Empaquetado de flujos junto con sus secretos requeridos (sin valores) para compartir entre instancias de Dédalo.
