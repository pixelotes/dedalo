# Documentación de Dédalo

Bienvenido a la documentación técnica y guía de arquitectura de **Dédalo**, el orquestador visual y compilador de flujos de trabajo ultraligero diseñado para ejecutarse de forma continua en hardware de recursos restringidos (como una Raspberry Pi con 15–35 MB de RAM).

---

## 📚 Índice de la Documentación

1. **[Filosofía y Arquitectura](filosofia_y_arquitectura.md)**
   - Misión y motivación del proyecto.
   - Por qué compilamos a Python plano en lugar de usar motores reactivos o colas pesadas.
   - Decisiones técnicas clave: ausencia de empaquetadores en frontend, modelo de ejecución determinista, gestión de secretos en disco (`0600`) y centinelas de omisión (`SKIPPED`).

2. **[Catálogo Completo de Nodos](catalogo_de_nodos.md)**
   - Catálogo exhaustivo de los **72 nodos disponibles**:
     - **27 Nodos de Entrada (`input.*`)**: Webhooks, llamadas API, Git, telemetría de incidentes, Jira, Confluence, WHOIS/RDAP, DNS DoH, sensores Pi/Linux, RSS, IMAP, etc.
     - **28 Nodos de Transformación (`transform.*`)**: Agentes LLM, Coder con ReAct, Analizador y Planificador SecOps, Auditoría CVE/SBOM, Escáner de Secretos (DLP), Extracción JSON Path, Mapeador de Datos, Puerta de Aprobación Humana (`transform.approval`), operadores de listas, diffs con hash, deduplicación difusa, regex, selectores CSS, sanitización y scripts Python.
     - **17 Nodos de Salida (`output.*`)**: Microsoft Teams, Bóveda de evidencia con gzip+sha256, operaciones Jira (crear, comentar, transicionar), páginas Confluence (crear, reemplazar, anexar), Git commits/push, SQLite, Telegram, Discord, Slack, MQTT, etc.

3. **[Agentes Nativos e Inteligencia Artificial](agentes_y_llms.md)**
   - Por qué evitamos frameworks inflados como LangChain, LangGraph o SDKs monolíticos.
   - El microarnés ReAct multi-turno (`transform.coder`) con herramientas reales (`bash`, `read`, `write`, `edit`, `grep`, `find`, `ls`).
   - Algoritmos de compactación de memoria: **Blackhole** y **Luhn+** para ventanas de contexto finitas.
   - Enmascaramiento de secretos en memoria (`sanitize_secrets`) para flujos de ciberseguridad e infraestructura.

4. **[Exportación Standalone y Tree-Shaking](exportacion_standalone.md)**
   - Cómo compilar cualquier grafo visual a un único fichero `.py` totalmente autónomo.
   - Algoritmo de *tree-shaking*: inclusión dinámica exclusiva de librerías utilizadas (`httpx`, `gzip`, `sqlite3`, etc.).
   - Modos de ejecución: interactivo, demonio cron y despliegue con Docker o Systemd.

5. **[Casos de Uso y Workflows Representativos](casos_de_uso_y_workflows.md)**
   - Diagramas paso a paso y flujos listos para producción:
     - **Triaje y Remediación de Kubernetes con Preservación de Evidencias**.
     - **Auditoría de Identidades: Detección de Usuarios Zombie en SaaS vs Entra ID**.
     - **Gestión Automática del Ciclo de Vida de Incidencias en Jira Cloud**.
     - **Documentación Viva en Confluence: Lectura, Extracción y Actualización en Espacios**.
     - **Briefing Matutino Seguro de Correo Electrónico (Anti-Inyección)**.
     - **Vigilancia de Infraestructura Edge (Raspberry Pi, Certificados SSL y Latencias)**.

6. **[Dependencias y Rendimiento](dependencias_y_rendimiento.md)**
   - Análisis detallado del stack tecnológico (FastAPI/Starlette, uvicorn, httpx, curl_cffi opcional).
   - Por qué no se utiliza Node.js, React, Webpack ni dependencias innecesarias.
   - Benchmarks y mediciones reales de memoria y consumo de CPU.
