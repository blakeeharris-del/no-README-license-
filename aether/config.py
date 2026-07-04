"""
aether.config
===============

Pydantic Settings, loaded from environment variables / ``.env``
(Phase-0 Prompt Section 7).

Per coding convention #9 ("all config via settings object. No
os.getenv() outside config.py."), this is the single place in the
codebase permitted to read environment variables directly (via
pydantic-settings). Every other module imports ``settings`` from here.
"""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application configuration, loaded from environment / .env file.

    Two separate DB connection strings, not one, per the architectural
    decision documented in Alembic migration 0001: ``database_url`` is
    the app's own runtime connection, authenticating as the
    privilege-restricted ``aether_app_role`` (so the DELETE/UPDATE
    REVOKEs from migrations 0002-0004 actually constrain the running
    app, not just a hypothetical test connection).
    ``migration_database_url`` authenticates as the unrestricted
    database owner and is used only by ``alembic/env.py``, since
    migrations need CREATE TYPE/TABLE/ROLE privileges
    ``aether_app_role`` intentionally lacks.
    """

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    database_url: str
    migration_database_url: str
    # Declared here so pydantic-settings doesn't reject the .env value
    # as an unrecognized field (case_sensitive=False + strict-by-default
    # extra="forbid" otherwise raises a ValidationError on startup).
    # Not actually read from this object anywhere — migration 0001
    # reads the same env var directly via os.environ, since migrations
    # run outside this Settings object's lifecycle. Kept here purely
    # so the app doesn't fail to start over a variable that legitimately
    # belongs to the same .env file.
    aether_app_db_password: str
    anthropic_api_key: str
    anthropic_model: str = "claude-sonnet-4-6"
    llm_max_retries: int = 3
    llm_context_token_budget: int = 8000
    # Not constrained to Literal["T0",...,"T4"] in the spec's own
    # sketch (plain `str = "T0"`), but trust maturity stages are a
    # fixed, closed set (Foundation §9.2) and INV-06 makes the model
    # non-configurable at runtime — tightening this to a Literal
    # catches a malformed .env value at startup instead of silently
    # accepting an invalid stage. Flagging as a minor deviation from
    # the literal spec sketch.
    aether_trust_stage: Literal["T0", "T1", "T2", "T3", "T4"] = "T0"
    synthesis_schedule_utc: str = "02:00"
    synthesis_threshold_nodes: int = 20
    watchdog_check_interval_ms: int = 1000
    default_skill_timeout_ms: int = 30000
    log_level: str = "INFO"


settings = Settings()
