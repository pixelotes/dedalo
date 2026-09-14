# Casos de Uso y Workflows Representativos en Dédalo

A continuación se detallan 6 casos de uso reales con sus diagramas de flujo de nodos, configuraciones recomendadas y arquitectura de datos.

---

## 1. Auditoría de Seguridad K8s con Preservación Forense

### Objetivo
Inspeccionar periódicamente el estado de un clúster de Kubernetes, preservar la evidencia original de forma inmutable mediante compresión y sellado criptográfico (para cumplimiento normativo o análisis forense) y notificar al equipo técnico con un diagnóstico y plan de acción.

```text
               ┌─── [input.command / script] ───┐
               │    (kubectl get pods -A ...)   │
               └──────────────┬─────────────────┘
                              │ Telemetría cruda
            ┌─────────────────┴─────────────────┐
            ▼                                   ▼
 [output.evidence_vault]              [transform.analyzer]
 (Gzip inmutable + SHA-256)           (Sanitiza secretos + Diagnóstico)
                                                │
                                                ▼
                                      [transform.planner]
                                      (Plan de mitigación estructurado)
                                                │
                                                ▼
                                      [output.jira_issue]
                                      (Abre incidencia con causa y plan)
```

### Configuración Paso a Paso:
1. **Extracción**: Un nodo `transform.script` ejecuta `kubectl` con un kubeconfig seguro montado en memoria, extrayendo los pods que no están en estado `Running`.
2. **Custodia de Evidencia (`output.evidence_vault`)**:
   - `vault_directory: "data/vault/k8s"`
   - Genera `k8s-evidence-20260914-1530.json.gz` y su manifiesto `.meta.json` con el hash SHA-256 inalterable.
3. **Análisis (`transform.analyzer`)**:
   - `sanitize_secrets: True` (garantiza que si un pod contenía un secreto en sus variables de entorno de depuración, se enmascare antes de ir al LLM).
   - Diagnostica causa raíz (ej. OOMKilled por límites de memoria insuficientes).
4. **Planificación (`transform.planner`)**:
   - Genera las recomendaciones de ajuste en los manifests de despliegue.
5. **Salida (`output.jira_issue`)**:
   - `action: "create"`
   - Crea un ticket de tipo "Task" o "Bug" en el proyecto de infraestructura con prioridad alta.

---

## 2. Triaje e Investigación Automatizada de Incidencias en Jira

### Objetivo
Supervisar la cola de incidencias abiertas en Jira Cloud, delegar la investigación inicial a un agente de triaje y actualizar cada ticket con un comentario detallado de resolución, ajustando la prioridad automáticamente.

```text
[input.jira_issues] 
  └─ JQL: project = SEC AND status = "Open"
         │
         ▼ Lista de tickets
[transform.analyzer]
  ├─ per_item: True
  ├─ as_field: "triaje"
  └─ prompt: "Investiga el síntoma, determina impacto y pasos de prueba."
         │
         ▼ Lote de tickets enriquecidos
[output.jira_issue]
  ├─ action: "add_comment" (o "transition")
  ├─ batch: True
  └─ comment: "{triaje}"
```

### Ventajas:
- **Cero frameworks pesados**: No requiere LangGraph ni orquestadores externos.
- **Procesamiento en lote nativo**: El nodo de salida detecta la lista enriquecida y ejecuta las peticiones REST correspondientes a Jira Cloud en formato Atlassian Document Format (ADF).

---

## 3. Revisión de Usuarios SaaS vs Entra ID (Detección de Usuarios Zombie)

### Objetivo
Realizar revisiones semanales de cuentas en servicios SaaS corporativos (GitHub, Jira, Salesforce, AWS) comparándolas con los usuarios activos en Microsoft Entra ID (Azure AD). Si un usuario dado de baja en la empresa sigue activo en el SaaS, se dispara una alerta de seguridad inmediata.

```text
[input.http_call] (API del SaaS: lista usuarios) ───┐
                                                    ├──► [transform.script]
[input.http_call] (Microsoft Graph API: Entra ID) ──┘    (Cruce de conjuntos en Python)
                                                               │
                                                               ▼
                                                     [transform.threshold]
                                                     (Total zombies > 0)
                                                               │
                                                               ▼
                                                     [output.telegram / ntfy]
                                                     (Alerta inmediata a CISO/IT)
```

### Código en `transform.script`:
```python
# Cruzar datos con conjuntos de Python en memoria
saas_data = inp.item("input.http_call_1") or []
entra_data = inp.item("input.http_call_2") or []

saas_emails = {u["email"].lower() for u in saas_data}
entra_active_emails = {u["userPrincipalName"].lower() for u in entra_data if u.get("accountEnabled")}

zombies = list(saas_emails - entra_active_emails)

return {
    "total_zombies": len(zombies),
    "zombies_list": zombies,
}
```

---

## 4. Documentación Viva en Confluence: Lectura y Actualización In-Place

### Objetivo
Mantener actualizada de forma continua una página de documentación de arquitectura o postura de seguridad en Confluence Cloud, leyendo el contenido previo, recalculando el nuevo estado y republicando la página sin sobrescribir información histórica de forma destructiva.

```text
[input.confluence_pages]
  ├─ space_key: "SEC"
  └─ title: "Postura de Seguridad Semanal"
          │
          ▼ (id, version, content_text)
[transform.analyzer / script]
  └─ Genera nuevos hallazgos e incorpora la sección actualizada
          │
          ▼
[output.confluence_page]
  ├─ mode: "create_or_update" (o "append")
  └─ Gestiona versión + 1 y Storage Format XHTML automáticamente
```

---

## 5. Briefing Matutino Seguro de Correo (Defensa Anti-Prompt-Injection)

### Objetivo
Procesar correos electrónicos entrantes durante la noche para generar un resumen diario ejecutivo, neutralizando intentos de ataque mediante *prompt injection* ocultos en texto invisible o código malicioso de marketing.

```text
[input.imap] ──────────► [transform.clean_email] ───► [transform.agent] ───► [output.telegram]
(Buzón no leídos)        - Quita texto oculto por CSS  (Resumen ejecutivo)    (Envío al móvil)
                         - Elimina caracteres zero-width
                         - Trunca firmas y disclaimers
```

---

## 6. Vigilancia de Hardware y Red en Raspberry Pi

### Objetivo
Monitorización desatendida 24/7 de un nodo doméstico o industrial. El flujo consume 0% de CPU la mayor parte del tiempo y se ejecuta cada 15 minutos comprobando temperatura, disco y caducidad de certificados SSL.

```text
[input.pi_health] (CPU temp y memoria) ──┐
[input.disk_free] (Espacio en /) ────────┼──► [transform.threshold] ──► [output.ntfy]
[input.ssl_cert]  (Días restantes TLS) ──┘    (Temp > 75 ó Disco < 10%     (Push instantáneo)
                                               ó Días TLS < 15)
```
Si los parámetros están dentro de la normalidad, `transform.threshold` emite el centinela `SKIPPED`, y el flujo finaliza sin generar tráfico de red ni alertas innecesarias.
