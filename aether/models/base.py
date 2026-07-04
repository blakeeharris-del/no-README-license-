"""
aether.models.base
===================

Shared SQLAlchemy 2.x DeclarativeBase for all Aether ORM models.

Lives in ``models`` (not ``database``) so that the models package has
no dependency on the database/engine layer, per the layering rule in
CLAUDE.md: ``memory/ imports from models/ only`` and models must not
import upward. ``aether/database.py`` (Step 4) imports ``Base`` from
here to bind the async engine and create all tables' metadata.

Every ORM model in this codebase uses explicit ``mapped_column(...)``
calls rather than SQLAlchemy's ``Annotated`` + ``type_annotation_map``
shortcut. This is a deliberate choice, not an oversight: with every
column's type, nullability, default, and FK spelled out at its
definition site, a reviewer can audit any single model file in
isolation without cross-referencing a type map defined elsewhere.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for all Aether ORM models."""

    pass
