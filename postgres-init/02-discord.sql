SELECT 'CREATE DATABASE mautrix_discord OWNER matrix TEMPLATE template0 ENCODING ''UTF8'' LC_COLLATE ''C'' LC_CTYPE ''C'''
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'mautrix_discord')
\gexec
