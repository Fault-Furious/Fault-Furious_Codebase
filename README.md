# Nordic Triage

Multi-agent incident diagnosis on a real running platform. Context: `BRIEF.md`.

## Run it

```sh
docker compose up -d --build   # whole platform + steady traffic
./check.sh                     # PASS = linked span/child/log/metric for one /login
docker compose down -v         # stop and wipe all data
```

Stockholm is at http://localhost:8001 and Helsinki at http://localhost:8002.
Telemetry DB is on `localhost:5433` (user/password/db `telemetry`).

## What runs

| Service | Machine (`source`) | What it is |
|---|---|---|
| `stockholm` | `stockholm` | App server (FastAPI), `app/` |
| `helsinki` | `helsinki` | Same image, different env |
| `userdb` | — | User database, Postgres 16 (Vantaa) |
| `dbmon` | `vantaa` | Telemetry for `userdb`: tails Postgres's CSV log + polls `pg_stat_activity` |
| `telemetrydb` | — | Where all telemetry lands |
| `loadgen` | — | Fake visitors, steady browse/search/login traffic on both servers |

Networks: each app server shares a network with the databases, and the two
servers share *only* the `nordic-peer` network. The home page calls the other
server's `/api/online` with a `traceparent` header, so traces cross machines.

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

Per-agent roles (stub, see P3): `agent_stockholm`, `agent_helsinki`, `agent_vantaa`,
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
| One read-only role per machine (stand-in until P3's lands) | Done, isolation tested |
| `./check.sh`, the done-criterion test | Passes |

P1 owns `otel.py`, `app/`, `dbmon/`, `loadgen/`, `faults/`, `docker-compose.yml` and
`check.sh`. Ask P1 for new telemetry fields, endpoints or faults.

## What each teammate does now

**Everyone first:** install Docker, then run `docker compose up -d --build` followed by
`./check.sh`. It should print `PASS`.

### P3: telemetry schema and roles
1. Replace the stand-in file `stub/telemetry-init.sh` with your own schema and roles.
2. Keep the table and column names above the same. If you must rename anything, tell P1
   first, because `otel.py` writes to them.
3. Decide these:
   - Add a real `trace_id` column to `metrics`? Today it sits inside `attrs`.
   - Add a forum column to `messages`? Without it, every forum page shows the same messages.
   - Data clean-up: traffic adds roughly 30 rows a second.
4. Keep the isolation rule true: each agent role can see only its own machine's rows.

### P4: agents
1. Build three agents, one per machine: `stockholm`, `helsinki` and `vantaa`.
2. Each agent logs in with its own role, for example
   `postgresql://agent_helsinki:agent@localhost:5433/telemetry`. Inside compose, use
   host `telemetrydb:5432` on the `default` network.
3. Never give an agent the `telemetry` admin login, because that breaks the whole idea
   of the project.
4. To get test data, run a fault for about 30 seconds, reset it, and read the rows from
   that time window. Normal traffic between faults gives you "nothing is wrong" examples.
5. To add your service to compose, go through P1. Or run it outside Docker against port 5433.

### P2: belief and merge logic
1. Combine the agents' findings using shared clues: trace ids, timestamps, error codes
   (`53300`, `PEER_UNREACHABLE`), and parent spans that one agent can't see but the
   other can.
2. These are the correct answers to test against:
   - **Surge:** the cause is the database (Vantaa refusing connections). The symptom is
     the app servers' failed SQL calls.
   - **Split:** both app servers report the other unreachable. The database is clean,
     which rules it out.
3. For the demo, cut the link between two agents and the system must decline to answer.

## The whole team must decide this week

During the surge, most of the app servers' error messages include the database's "too
many clients" text. The app agent therefore sees *what* is failing, though not *why*. Is
that split enough for the demo, or should the app log only "DB connection failed"?

Note: `PASSWORD_HASH_ITERATIONS` defaults to 1000 so that the DB, not app CPU, is the
surge bottleneck.
