# Agentes Nativos e Inteligencia Artificial en Dédalo

## 1. Por qué evitamos LangChain, LangGraph y Frameworks Pesados

En muchas soluciones de automatización con IA, es común encontrar dependencias de frameworks como **LangChain**, **LangGraph**, **LlamaIndex** o los SDKs oficiales de OpenAI. Aunque populares, estos paquetes presentan graves inconvenientes para sistemas de producción ligeros:

1. **Inflación masiva de dependencias**: Instalar LangChain y sus complementos añade cientos de paquetes a `site-packages` (Pydantic, SQLAlchemy, Tenacity, tiktoken, etc.), aumentando el tamaño del entorno virtual en más de 350 MB y disparando la huella de memoria RAM inicial.
2. **Abstracciones frágiles y obsolescencia rápida**: Las continuas roturas de API entre versiones menores obligan a reescribir código y complican el mantenimiento a largo plazo.
3. **Pérdida de control sobre los prompts**: Muchos frameworks inyectan prompts predeterminados ocultos que desaprovechan tokens de la ventana de contexto y añaden latencia.

**La solución de Dédalo:**
- Dédalo se comunica directamente con cualquier servidor LLM mediante el protocolo estándar HTTP REST compatible con OpenAI (`/v1/chat/completions`) utilizando el cliente `httpx`.
- Esto permite conectar con la misma simplicidad a:
  - **Modelos locales**: Ollama, vLLM, llama.cpp, LocalAI o LM Studio.
  - **Servicios en la nube**: Groq, DeepSeek, OpenAI, Mistral, Together AI, etc.
- Todo el código de llamada al LLM, bucle ReAct y manejo de herramientas está contenido en menos de 400 líneas de código limpio y probado en `dedalo/runtime.py`.

---

## 2. Los 4 Nodos de Agente en Dédalo

Dédalo ofrece cuatro nodos especializados según la tarea requerida:

```text
┌────────────────────────────────────────────────────────────────────────┐
│                        Nodos de Agente en Dédalo                       │
├───────────────────┬───────────────────┬────────────────────────────────┤
│ Nodo              │ Propósito         │ Características Clave          │
├───────────────────┼───────────────────┼────────────────────────────────┤
│ transform.agent   │ Tareas Generales  │ Modo JSON, variable {entrada}, │
│                   │ de Razonamiento   │ enriquecimiento `as_field`     │
├───────────────────┼───────────────────┼────────────────────────────────┤
│ transform.coder   │ Programación y    │ Bucle ReAct, bash, lectura y   │
│                   │ Operaciones CLI   │ edición atómica, Blackhole     │
├───────────────────┼───────────────────┼────────────────────────────────┤
│ transform.analyzer│ SecOps, SRE y     │ Enmascaramiento de secretos,   │
│                   │ Diagnóstico       │ proceso por lotes (per_item)   │
├───────────────────┼───────────────────┼────────────────────────────────┤
│ transform.planner │ Planes de Acción  │ Planes estructurados (Markdown │
│                   │ y Remediación     │ o JSON) con mitigación y rollb │
└───────────────────┴───────────────────┴────────────────────────────────┘
```

---

## 3. `transform.coder`: Microarnés ReAct con Herramientas Reales

El nodo `transform.coder` no es un simple generador de texto; es un **agente autónomo interactivo** diseñado para resolver problemas de código y administración de sistemas:

### 3.1. Herramientas Disponibles y Gestión de Permisos
El agente dispone de una lista de herramientas nativas según el `tools_preset` o la configuración personalizada de permisos en el inspector:
- **`read`**: Lee archivos completos o rangos específicos de líneas con numeración clara.
- **`bash`**: Ejecuta comandos reales en la consola del servidor (dentro del `workspace_dir`) con límite de tiempo y captura de stdout/stderr.
- **`edit`**: Realiza reemplazos atómicos de cadenas (`old_str` -> `new_str`) exigiendo que la ocurrencia sea única para evitar ediciones ambiguas.
- **`write`**: Escribe ficheros nuevos en disco.
- **`grep`**: Búsqueda rápida de expresiones o patrones dentro del árbol de trabajo.
- **`find` / `ls`**: Exploración rápida de la jerarquía de ficheros y directorios.
- **`web_search`**: Búsqueda de documentación y soluciones en internet mediante DuckDuckGo (sin necesidad de API keys).

#### Gestión Granular de Permisos desde el Inspector
En el panel lateral de ajustes del nodo, el operador puede seleccionar perfiles predefinidos (`Programación`, `Solo Lectura`, `Todas`, `A Medida`) o activar/desactivar permisos concretos agrupados por nivel de riesgo:
- **Lectura y Navegación** (Seguro): `read`, `grep`, `find`, `ls`.
- **Modificación en Disco** (Escritura): `edit`, `write`.
- **Ejecución en Consola** (Shell): `bash`.
- **Acceso a Red / Internet**: `web_search`.

### 3.2. Compactación de Contexto (Blackhole y Luhn+)
Cuando un agente interactúa durante múltiples turnos, el historial de mensajes crece rápidamente y puede desbordar la ventana de contexto del modelo.
- Dédalo autodetecta el tamaño de la ventana de contexto del modelo (`context_window`).
- Si los tokens acumulados alcanzan el **90%** de la ventana, entra en juego el motor de compactación seleccionado:
  - **Blackhole (Determinista)**: Sintetiza el historial sin llamadas adicionales al LLM, preservando la instrucción original del usuario, las herramientas invocadas y sus códigos de salida, purgando salidas intermedias verbosas.
  - **Luhn+ (Algorítmico Ponderado)**: Extrae las frases más representativas basándose en la frecuencia y posición de las palabras clave.

---

## 4. `transform.analyzer` y Enmascaramiento de Secretos (`sanitize_secrets`)

En operaciones de seguridad (SecOps) e investigación de infraestructura, los logs y trazas a menudo contienen tokens de autenticación, credenciales o claves privadas. Enviar estos datos a un modelo externo supone un riesgo crítico de fuga de información.

Cuando `sanitize_secrets: True` está activado en `transform.analyzer`:
- El nodo ejecuta una función de sanitización en memoria (`_mask_secrets_in_text`) antes de construir el payload para el LLM.
- Se enmascaran automáticamente:
  - Cabeceras `Authorization: Bearer <token>` y tokens JWT (`ey...`).
  - Claves privadas PEM (`-----BEGIN PRIVATE KEY-----`).
  - Secretos y claves de AWS (`AKIA...`), GitHub (`ghp_...`, `ghs_...`), Slack (`xoxb-...`).
  - Contraseñas y tokens en URLs (`https://user:password@host`).
  - Direcciones IPv4 privadas y credenciales Atlassian.

### Procesamiento por Lote (`per_item`)
Si la entrada es una lista de tickets de Jira (`input.jira_issues`) o páginas de Confluence (`input.confluence_pages`), `transform.analyzer` puede iterar sobre cada elemento de forma secuencial, diagnosticarlo individualmente e inyectar el análisis directamente dentro de cada registro en el campo indicado por `as_field` (ej. `item["analysis"] = "..."`).

---

## 5. `transform.planner`: Mitigación Estructurada

El nodo `transform.planner` actúa como el paso complementario al analizador:
- Recibe el diagnóstico del incidente y los metadatos del recurso.
- Produce un plan de remediación estructurado que contempla:
  1. **Acciones de Mitigación Inmediata** (para detener el impacto).
  2. **Resolución Definitiva** (para subsanar la causa raíz).
  3. **Plan de Retroceso (*Rollback*)** (en caso de que el arreglo falle).
  4. **Pruebas de Verificación**.
- Puede emitir el resultado en Markdown listo para adjuntar a un ticket de Jira o una página de Confluence, o en formato JSON estricto para su procesamiento automatizado posterior.
