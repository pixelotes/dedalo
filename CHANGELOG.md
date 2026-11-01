# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/).

## [Sin publicar]

### Añadido

- Token opcional de webhook (`DEDALO_WEBHOOK_TOKEN`), límite de cuerpo (`DEDALO_MAX_BODY_MB`) y cabeceras de seguridad con CSP.
- Cifrado de secretos en reposo, filtrado de entorno y aislamiento opcional de comandos con bubblewrap.
- Validación de `Host` (`DEDALO_ALLOWED_HOSTS`) y de `Origin`; se elimina CORS.

### Cambiado

- Los webhooks se ejecutan en un hilo y ya no bloquean el servidor.
- `/api/history/purge` exige `days` entre 1 y 3650.
- `list_flows` usa una caché por fecha y tamaño de fichero, los errores 500 de guardado, importación y exportación no exponen detalles internos y el arranque es ~100 ms más rápido (imports perezosos).
- El planificador ejecuta flujos en paralelo (`DEDALO_MAX_CONCURRENT_FLOWS`, 2 por defecto).
- La base SQLite del historial se inicializa una sola vez por proceso.
- Las acciones de TruffleHog y Trivy del CI se fijan por SHA.

### Añadido (higiene)

- Licencia Elastic License 2.0, `NOTICE`, política de seguridad y guía de contribución.
