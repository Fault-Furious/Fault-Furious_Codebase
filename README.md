# Nordic Triage

Multi-agent incident diagnosis on a real running platform. Context: `BRIEF.md`.

## Run it

```sh
docker compose up -d --build   # platform, telemetry DB, and three agents
./check.sh                     # PASS = linked span/child/log/metric for one /login
agent/check.sh stack           # DB isolation and agent convergence checks
docker compose down            # stop services (keeps database data)
```

For a public reviewer deployment, see [`DEPLOY.md`](DEPLOY.md). It uses a
production Compose overlay and HTTPS reverse proxy; do not expose the database
or agent API ports publicly.

Stockholm is at http://localhost:8001 and Helsinki at http://localhost:8002.
Telemetry DB is on `localhost:5433` (user/password/db `telemetry`).
Agents are on `http://localhost:9001` (Stockholm), `:9002` (Helsinki), and
`:9003` (Vantaa). Each agent reads telemetry through its own read-only role and
exchanges belief state with the other two.

## What runs

| Service | Machine (`source`) | What it is |
|---|---|---|
| `stockholm` | `stockholm` | App server (FastAPI), `app/` |
| `helsinki` | `helsinki` | Same image, different env |
| `userdb` | — | User database, Postgres 16 (Vantaa) |
| `dbmon` | `vantaa` | Telemetry for `userdb`: tails Postgres's CSV log + polls `pg_stat_activity` |
| `telemetrydb` | — | Where all telemetry lands |
| `loadgen` | — | Fake visitors, steady browse/search/login traffic on both servers |
| `agent-stockholm`, `agent-helsinki`, `agent-vantaa` | matching source | Mock diagnosis, cross-checking, and belief-state gossip |

Networks: each app server has its own database network; the servers share only
`nordic-peer`. Agents share `nordic-agent-peer` for gossip and a separate
network to reach the telemetry database. The home page calls the other server's
`/api/online` with a `traceparent` header, so traces cross machines.

Start an investigation on all agents with the same incident ID. For example:

```sh
for port in 9001 9002 9003; do
  curl -s "http://localhost:$port/incident" -H 'content-type: application/json' \
    -d '{"id":"demo-surge","scenario":"surge"}'
done
curl -s http://localhost:9001/state
```

Each `/state` response includes a digest; once gossip settles, all three digests
match. The mock distinguishes the surge cause (Vantaa's `53300` connection
refusals) from the app symptom (failed SQL spans/logs). In a network split, the
app agents report `PEER_UNREACHABLE` while Vantaa reports a healthy database.

## Interface: what lands in the telemetry DB

Every row carries `source` = `stockholm` | `helsinki` | `vantaa`.

```
traces   span_id, trace_id, parent_id, ts, name, target, duration_ms, status, source, attrs
metrics  id, ts, metric, value, unit, source, attrs
logs     id, ts, severity, source, trace_id, message, code
```

- **Spans.** `target` is `http` (request, root span), `userdb` (one per SQL call) or
  `peer` (call to the other server). `status` is `OK` or `ERROR`.
- **Span attrs.** `device` (user agent), `status_code`, `username`, `forum`, `page`, `sqlstate`, `error`.
- **Cross-server parents.** Helsinki's `GET /api/online` span has `parent_id` = a
  Stockholm `peer online` span, and the reverse. Each agent sees only its own side, so
  that parent never resolves for it.
- **Metric `trace_id`.** The metrics table has no trace column, so app metrics put it in `attrs->>'trace_id'`.
- **Log `code`.** It holds the SQLSTATE where Postgres gave one (`53300`, `23505`), and
  `PEER_UNREACHABLE` for failed peer calls.

| metric | source | unit |
|---|---|---|
| `http.server.duration` | apps | ms, attrs `path`, `status_code` |
| `login.attempts` | apps | count, attrs `ok` |
| `signups` | apps | count |
| `db.connections` / `.active` / `.max` | vantaa | count, every second |
| `db.connections.refused` | vantaa | count of 53300s in the last second |

Per-agent roles (from `telemetry/init.sh`, P3): `agent_stockholm`, `agent_helsinki`, `agent_vantaa`,
password `agent`. Row-level security limits each role to its own `source`:

```sh
psql postgresql://agent_helsinki:agent@localhost:5433/telemetry -c "select source, count(*) from traces group by 1"
```

## Faults

```sh
faults/signup_surge.sh            # userdb max_connections=10, 150 signups/s per server
faults/signup_surge.sh reset

faults/split.sh                   # cut Stockholm <-> Helsinki
faults/split.sh reset
```

What each fault produces (seen in testing; signals show up within about 10 seconds):

- **Surge.** `vantaa` logs thousands of `53300` ("sorry, too many clients already") and
  `db.connections.refused` climbs. Stockholm and Helsinki log `insert user failed:
  connection failed ...` at ERROR, with `userdb` spans in `ERROR`.
- **Split.** Both apps log `PEER_UNREACHABLE` and `peer online` spans go `ERROR`.
  `vantaa` shows nothing unusual, and there are no DB errors anywhere.

## Rules (from the brief)

- One owner per file, so ask before you edit someone else's.
- Services only write telemetry through `otel.py` (`start_span`, `log`, `metric`). No direct inserts.
- All config comes from env vars, with defaults in `docker-compose.yml`.
- `docker compose up` must work on main.

## Platform status (P1)

| Piece | Status |
|---|---|
| `otel.py`, the telemetry module (spans, logs, metrics, background writes) | Done |
| App server Stockholm (`localhost:8001`), all 4 pages | Done |
| App server Helsinki (`localhost:8002`), same code, different env | Done |
| Trace ids carried between the two servers | Done |
| User database (Postgres) and its telemetry (`dbmon`, source `vantaa`) | Done |
| Load generator (steady fake traffic on both servers) | Done |
| Fault 1, signup surge: `faults/signup_surge.sh` | Done and tested |
| Fault 2, network split: `faults/split.sh` | Done and tested |
| One read-only role per machine | Done, isolation tested |
| Three agent services in the default Compose stack | Done |
| Belief merge and gossip convergence for surge, split, and quiet traffic | Done, tested |
| `./check.sh`, the done-criterion test | Passes |

P1 owns `otel.py`, `app/`, `dbmon/`, `loadgen/`, `faults/`, `docker-compose.yml` and
`check.sh`. Ask P1 for new telemetry fields, endpoints or faults.

## What each teammate does now

**Everyone first:** install Docker, then run `docker compose up -d --build` followed by
`./check.sh`. It should print `PASS`.

### P3: telemetry schema and roles
Done: `telemetry/init.sh` (schema, roles, views, `telemetry_prune`) and `telemetry/check.sh`.
Services write as `telemetry_writer`, which can insert but not read. Open: a forum column on
`messages`, and whether `telemetry_prune()` runs on a timer.

### P4: agents
Three agents run in the default Compose stack. Each has a distinct `NODE_ID`, a
private database role, and `/incident`, `/exchange`, `/state`, `/health` endpoints.
The current `AGENT_MODE=mock` uses deterministic claims while keeping the same
`analyse`/`evaluate` interface planned for the model implementation. See
[`agent/README.md`](agent/README.md) for endpoint and fault-check details.

### P2: belief and merge logic
`belief.py` keys claims by normalized content hash, unions evidence IDs, and
merges each agent's confidence through a versioned register. `agent/check.sh all`
tests the convergence gate and the two live faults. The expected diagnosis is:

- **Surge:** Vantaa's connection refusals (`53300`) explain the cause; failed SQL
  spans and logs on Stockholm/Helsinki are the symptom.
- **Split:** Stockholm and Helsinki report `PEER_UNREACHABLE`; Vantaa reports no
  database connection errors.

## The whole team must decide this week

During the surge, most of the app servers' error messages include the database's "too
many clients" text. The app agent therefore sees *what* is failing, though not *why*. Is
that split enough for the demo, or should the app log only "DB connection failed"?

Note: `PASSWORD_HASH_ITERATIONS` defaults to 1000 so that the DB, not app CPU, is the
surge bottleneck.
