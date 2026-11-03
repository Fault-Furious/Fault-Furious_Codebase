#!/bin/sh
# User database schema. Apps connect as a non-superuser so Postgres's reserved
# superuser slots stay free for dbmon even when clients are being refused (53300).
set -e
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<SQL
CREATE ROLE $APP_DB_USER LOGIN PASSWORD '$APP_DB_PASSWORD';
CREATE TABLE users (
  id SERIAL PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  password TEXT NOT NULL,
  email TEXT NOT NULL
);
CREATE TABLE messages (
  id SERIAL PRIMARY KEY,
  message TEXT NOT NULL,
  username TEXT NOT NULL
);
INSERT INTO messages (message, username)
SELECT 'Hei maailma #' || g, 'seed' FROM generate_series(1, 250) g;
GRANT SELECT, INSERT, UPDATE, DELETE ON users, messages TO $APP_DB_USER;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO $APP_DB_USER;
SQL
