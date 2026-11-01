# Contributing to Dédalo

Thanks for your interest. Before opening a large PR, open an issue to discuss the idea.

## Development environment

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Requires Python 3.11 or later.

## Before submitting a change

```bash
ruff check .
pytest -v
```

CI runs the same checks (plus secret and vulnerability scanning); the PR must pass.

## Conventions

- Code, documentation and commit messages in English (Spanish is supported as a UI and message language).
- Keep the core lightweight: avoid adding dependencies unless they are essential.
- Behavior changes come with a test in `tests/`.
- If you change nodes, the API or environment variables, update `README.md` and `doc/`.
- Never include secrets, keys or the contents of `data/` in commits or screenshots.

## Vulnerabilities

Do not use public issues. Follow [SECURITY.md](SECURITY.md).

## License

By contributing you agree that your contribution will be distributed under the project's [Elastic License 2.0](LICENSE).
