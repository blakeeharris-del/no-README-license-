Aether — personal AI OS. Python 3.11+, FastAPI, PostgreSQL 15+,
SQLAlchemy 2.x async, Alembic, Anthropic claude-sonnet-4-6.

Phase-0 only. No external connectors. No frontend. Mock data only.

INV-01 through INV-10 are runtime constraints. Not negotiable.

Action Gateway is the only external execution path.

agents/ imports from skills/ only.
skills/ imports from memory/ and models/ only.
memory/ imports from models/ only.

Cross-layer imports are architecture violations.
