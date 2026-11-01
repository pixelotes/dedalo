# Contribuir a Dédalo

Gracias por tu interés. Antes de abrir un PR grande, abre un issue para comentar la idea.

## Entorno de desarrollo

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Requiere Python 3.11 o superior.

## Antes de enviar un cambio

```bash
ruff check .
pytest -v
```

CI ejecuta lo mismo (más escaneo de secretos y vulnerabilidades); el PR debe pasar en verde.

## Convenciones

- Código y mensajes de commit en español, como el resto del repositorio.
- Mantén el núcleo ligero: evita añadir dependencias salvo que sean imprescindibles.
- Los cambios de comportamiento llevan su prueba en `tests/`.
- Si cambias nodos, API o variables de entorno, actualiza `README.md` y `doc/`.
- Nunca incluyas secretos, claves ni contenido de `data/` en commits o capturas.

## Vulnerabilidades

No uses issues públicos. Sigue [SECURITY.md](SECURITY.md).

## Licencia

Al contribuir aceptas que tu aportación se distribuya bajo la [Elastic License 2.0](LICENSE) del proyecto.
