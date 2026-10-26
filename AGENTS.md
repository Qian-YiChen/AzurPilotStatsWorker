# Repository Guidelines

## Project Structure & Module Organization

This Python service implements node B of the screenshot statistics pipeline. `worker/app.py` exposes authenticated WebSocket and preview endpoints. `worker/db.py` owns SQLite durability, `clients.py` handles C and 123Pan, `engine.py` schedules work, and `recognize.py` adapts the external Alas parser in a subprocess. Tests live in `tests/`; Tunnel examples belong in `deploy/`. Runtime SQLite files and temporary originals stay in ignored `data/`.

## Build, Test, and Development Commands

- `uv sync --locked`: install reproducible runtime and development dependencies.
- `uv run python -m worker.app`: start with configuration loaded into the environment.
- `uv run pytest -q`: test persistence, authentication, retries, and cleanup.
- `uv run pytest tests/test_alas_adapter.py -q`: verify real OCR when `ALAS_ROOT` and `ALAS_PYTHON` are configured.
- `uv run python -m compileall -q worker`: check Python syntax.

## Coding Style & Naming Conventions

Use four-space indentation, descriptive snake_case functions/modules, and PascalCase classes. Keep transport, persistence, and image recognition responsibilities separate. Serialize only native JSON types; convert NumPy scalars explicitly. No formatter is currently enforced. Update `uv.lock` when dependencies change.

## Testing Guidelines

Use pytest, temporary directories, and FastAPI's TestClient. Name tests `test_<behavior>`. Mock external HTTP services in regular tests. Optional OCR tests use existing client assets to construct screenshots; never commit player screenshots or credentials. Include restart, duplicate, incomplete-recognition, and delivery-failure scenarios for protocol changes.

## Commit & Pull Request Guidelines

Use focused `feat:`, `fix:`, or `docs:` messages. Explain observable behavior, external parser compatibility, and validation results. Record optional test skips. Create a local commit after completing and validating work unless instructed otherwise.

## Storage & Configuration

Only one process may own a data directory. Acknowledge batches after commit. Keep structured results during C outages, delete temporary originals on every exit path, and never delete 123Pan originals. Pin template packages per task and keep all service credentials out of Git.
