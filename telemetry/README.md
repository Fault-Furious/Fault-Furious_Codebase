# Telemetry database

`init.sh` creates the telemetry schema, writer role and per-agent read roles. Each
row is tagged with a machine `source`; PostgreSQL row-level security restricts
each agent to rows from its own machine.

## Tables

```text
machines  source, location, ip, role, stack
traces    span_id, trace_id, parent_id, ts, name, target, duration_ms, status, source, attrs
metrics   id, ts, metric, value, unit, source, attrs, trace_id
logs      id, ts, severity, source, trace_id, message, code
```

`metrics.trace_id` is generated from `attrs->>'trace_id'`. The telemetry writer
continues to send the trace ID in `attrs`; agents and checks can join metrics to
traces through the generated column.

The database also provides the `http_requests`, `page_views`, `visitors`, and
`log_codes` views. They run with the reader's permissions, so row-level security
continues to apply. See `init.sh` for their exact columns and definitions.

## Roles

| Role | Permissions |
|---|---|
| `agent_stockholm`, `agent_helsinki`, `agent_vantaa` | Read only rows for the matching `source` from tables and views |
| `telemetry_writer` | Insert traces, metrics and logs; cannot read telemetry |
| `telemetry` | Database owner, used for administration only |

Local Compose defaults are for the demo only. Set unique passwords in `.env` for
any shared or public deployment; never give the owner credentials to an agent.

## Retention

`telemetry_prune(interval)` deletes rows older than the requested interval. It is
not scheduled automatically; run it manually as the database owner when needed:

```sql
SELECT * FROM telemetry_prune('6 hours');
```

## Initialization and checks

Compose mounts `init.sh` into the telemetry Postgres container. Postgres runs the
script only when initializing an empty data volume. If you change the schema,
back up any data before recreating the volume; `docker compose down -v` deletes
the local demo databases.

With the stack running, check source isolation, writer permissions and trace
links using:

```sh
telemetry/check.sh
```
