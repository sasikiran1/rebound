"""Connection settings for the runner. Local-dev defaults match docker/compose.yml."""

import os

PG_HOST = os.environ.get("REBOUND_PG_HOST", "127.0.0.1")
PG_DIRECT_PORT = int(os.environ.get("REBOUND_PG_PORT", "55432"))
PG_PROXY_PORT = int(os.environ.get("REBOUND_PG_PROXY_PORT", "15432"))

ADMIN_USER = "postgres"
ADMIN_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "postgres")
APP_USER = "fielddesk"
APP_PASSWORD = os.environ.get("FIELDDESK_DB_PASSWORD", "fielddesk")
ORACLE_USER = "oracle_ro"
ORACLE_PASSWORD = os.environ.get("ORACLE_DB_PASSWORD", "oracle")

TEMPLATE_DB = "fielddesk_template"
TRIAL_DB = os.environ.get("FIELDDESK_DB", "fielddesk_trial")

FIELDDESK_URL = os.environ.get("REBOUND_FIELDDESK_URL", "http://127.0.0.1:8080")
TOXIPROXY_URL = os.environ.get("REBOUND_TOXIPROXY_URL", "http://127.0.0.1:8474")
BACKEND_LOG = os.environ.get("REBOUND_BACKEND_LOG", "var/fielddesk/backend.jsonl")
TEMPLATE_MANIFEST = "data/build/template.json"


def admin_dsn(database: str = "postgres") -> str:
    return f"postgresql://{ADMIN_USER}:{ADMIN_PASSWORD}@{PG_HOST}:{PG_DIRECT_PORT}/{database}"


def oracle_dsn(database: str = TRIAL_DB) -> str:
    return f"postgresql://{ORACLE_USER}:{ORACLE_PASSWORD}@{PG_HOST}:{PG_DIRECT_PORT}/{database}"


def proxied_app_dsn(database: str = TRIAL_DB) -> str:
    """Through toxiproxy, as FieldDesk sees the database. Used by the fault canary."""
    return f"postgresql://{APP_USER}:{APP_PASSWORD}@{PG_HOST}:{PG_PROXY_PORT}/{database}"
