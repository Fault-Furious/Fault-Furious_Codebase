# Telemetry DB (P3)

Schema and roles for the telemetry database, in `init.sh`. It replaces the stand-in
`stub/telemetry-init.sh`. The agreed tables and columns are unchanged, so `otel.py` and
`agent/shard.py` work as before. Everything below is an addition.

The rule this file exists for: **an agent role sees only its own machine's rows.**
Row-level security in Postgres enforces it, on the tables and on every view.

## Tables

```
machines  source, location, ip, role, stack                 (new)
traces    span_id, trace_id, parent_id, ts, name, target, duration_ms, status, source, attrs
metrics   id, ts, metric, value, unit, source, attrs, trace_id   (trace_id is new)
logs      id, ts, severity, source, trace_id, message, code
```

- **`machines`** has one row per source, with the fixed IPs from the spec:
  `stockholm` 170.57.176.88, `helsinki` 104.172.209.255, `vantaa` 88.211.152.184.
  Every telemetry row's `source` must be in it, so a misspelt source fails loudly
  instead of making rows no agent can see.
- **`metrics.trace_id`** is generated from `attrs->>'trace_id'`, so `otel.py` didn't have to
  change. Join metrics to traces directly: `metrics JOIN traces USING (trace_id)`.
- There are no CHECKs on `severity` or `status`. `otel.py` retries a failed batch forever,
  so one odd value would stop all of a server's telemetry.
- Extra indexes cover `parent_id`, `trace_id` on logs and metrics, and `code`, for following
  traces and error codes.

## Views

All views run with the reader's rights (`security_invoker`), so an agent gets only its own rows.

| view | one row per | columns |
|---|---|---|
| `http_requests` | HTTP request (root span) | `method`, `path`, `route`, `status_code`, `device_type`, `user_agent`, `username`, `forum`, … |
| `page_views` | minute, source, route, device | `views`, `server_errors`, `avg_ms` (GET pages only, no API calls, no bots) |
| `visitors` | minute, source | `new_visitors` (signups), `returning_visitors` (successful logins), `failed_logins` |
| `log_codes` | minute, source, severity, code | `n` (WARN and above: `53300`, `PEER_UNREACHABLE`, …) |

`route` groups all forums as `/{forum}`. `device_type(user_agent)` returns `mobile`,
`tablet`, `desktop`, `bot` or `unknown`. Docker's healthcheck shows up as `bot`.

```sh
psql postgresql://agent_stockholm:agent@localhost:5433/telemetry \
  -c "select * from page_views order by minute desc limit 10"
```

## Roles

| role | can |
|---|---|
| `agent_stockholm`, `agent_helsinki`, `agent_vantaa` | `SELECT` own `source` only, on tables and views. Password `AGENT_DB_PASSWORD` (default `agent`) |
| `telemetry_writer` | `INSERT` into traces, metrics and logs. Cannot read. Password `TELEMETRY_WRITER_PASSWORD` (default `writer`) |
| `telemetry` (owner) | everything. Never give it to an agent |

One agent role is created per name in `AGENT_SOURCES`. Nothing else in the database is
open to `PUBLIC`.

## Clean-up

Traffic adds roughly 30 rows a second (about 2.6 M a day). As the owner:

```sql
SELECT * FROM telemetry_prune('6 hours');   -- deletes older rows, returns counts per table
```

Nothing runs this on a timer yet. For the demo, `docker compose down -v` wipes everything anyway.

## Run it

`telemetrydb` in `docker-compose.yml` must mount this file:

```yaml
- ./telemetry/init.sh:/docker-entrypoint-initdb.d/01.sh:ro
```

Postgres runs init scripts only on an empty volume, so after any change here:

```sh
docker compose down -v && docker compose up -d --build
sleep 10; ./check.sh && telemetry/check.sh
```

`telemetry/check.sh` checks isolation per role (tables and views), that agents can't write
or prune, that the writer can't read, that unknown sources are rejected, and that
`metrics.trace_id` joins traces. The last line is `PASS` or `FAIL`. It stops early if the
DB was still built from the stub.

## The open questions from the main README

- **Real `trace_id` on `metrics`:** done, as a generated column.
- **Forum column on `messages`:** not P3's. `messages` is in the user DB (`app/userdb-init.sh`)
  and the app's queries would change too, so it's P1's call.
- **Data clean-up:** `telemetry_prune()`, run by hand or on a timer.
- **Isolation:** still true, and tested by `telemetry/check.sh`.

## Needs someone else

- **P1:** switch `telemetrydb` to this file in `docker-compose.yml` (one line, above), then
  delete `stub/`.
- **P1, optional:** point the services' `TELEMETRY_DSN` at `telemetry_writer` instead of the
  owner. Also decide whether `telemetry_prune()` runs on a timer.
- **P4, optional:** select `trace_id` in the metrics query in `agent/shard.py`.
