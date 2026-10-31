# Política de seguridad

## Versiones soportadas

Solo se mantiene la rama `main`. Las correcciones de seguridad no se portan a versiones anteriores.

## Reportar una vulnerabilidad

No abras un issue público. Usa el reporte privado de GitHub:
**Security → Report a vulnerability** en este repositorio.

Incluye, si puedes:

- Versión o commit afectado y forma de despliegue (`run.sh`, Docker, systemd).
- Pasos para reproducirlo o una prueba de concepto.
- Impacto que estimas.

Respuesta inicial en un plazo de 7 días. Coordinaremos la publicación del aviso cuando haya corrección.

## Modelo de amenazas

Dédalo es una herramienta de automatización autoalojada que **ejecuta código** con los permisos del usuario que la ejecuta y **no incluye autenticación**. Está diseñada para escuchar en `localhost` o en una red privada de confianza (Tailscale, VPN, túnel SSH, proxy inverso con autenticación).

Fuera de alcance:

- Ejecución de código por quien ya tiene acceso a la interfaz o a la API: es la funcionalidad prevista.
- Despliegues expuestos a internet sin un proxy con autenticación.
- Flujos importados de terceros que ejecuten código malicioso.

Dentro de alcance, entre otros:

- Ejecución de código o lectura de secretos sin acceso a la interfaz (por ejemplo, desde otra web visitada en el navegador, desde el contenido procesado por un flujo o mediante prompt injection hacia `transform.agent` o `transform.coder`).
- Escritura o lectura de ficheros fuera de `data/`.
- Exposición de secretos en logs, historial o exportaciones.
