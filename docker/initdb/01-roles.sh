#!/bin/bash
# Roles: fielddesk (API, read-write on trial DBs), oracle_ro (read-only verdicts).
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<SQL
CREATE ROLE fielddesk LOGIN PASSWORD '${FIELDDESK_DB_PASSWORD}';
CREATE ROLE oracle_ro LOGIN PASSWORD '${ORACLE_DB_PASSWORD}';
ALTER ROLE oracle_ro SET default_transaction_read_only = on;
SQL
