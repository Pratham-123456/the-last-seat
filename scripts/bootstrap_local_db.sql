-- One-time, NON-DESTRUCTIVE bootstrap for the local PostgreSQL server.
-- Run as a superuser (e.g. postgres) via scripts/bootstrap_local_db.ps1.
--
-- * Creates a low-privilege login role `last_seat_app` (no SUPERUSER, no CREATEDB, no CREATEROLE).
-- * Creates last_seat_db and last_seat_test_db owned by that role, ONLY if they do not exist yet.
-- * Never drops, truncates or alters any other database.
--
-- The password is passed in with:  psql -v app_password=...   (never stored in this file)

\set ON_ERROR_STOP on

SELECT format('CREATE ROLE last_seat_app LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'last_seat_app')
\gexec

-- If the role already existed, (re)set its password so it matches the project's .env
SELECT format('ALTER ROLE last_seat_app WITH LOGIN PASSWORD %L', :'app_password')
\gexec

SELECT 'CREATE DATABASE last_seat_db OWNER last_seat_app ENCODING ''UTF8'''
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'last_seat_db')
\gexec

SELECT 'CREATE DATABASE last_seat_test_db OWNER last_seat_app ENCODING ''UTF8'''
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'last_seat_test_db')
\gexec

-- Report what exists now (read-only)
SELECT d.datname, r.rolname AS owner
FROM pg_database d JOIN pg_roles r ON r.oid = d.datdba
WHERE d.datname IN ('last_seat_db', 'last_seat_test_db')
ORDER BY d.datname;
