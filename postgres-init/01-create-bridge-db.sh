#!/bin/sh
# Runs only on first init of an empty volume. Creates the bridge DB with the
# cluster's C/C/UTF8 locale (set via POSTGRES_INITDB_ARGS).
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE DATABASE mautrix_whatsapp OWNER matrix TEMPLATE template0
        ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C';
EOSQL
