# Nordic Triage — platform owner brief

Read this first. It is the full context for the build.

## What the project is

Multi-agent incident diagnosis. Three AI agents, each able to read the telemetry
of exactly one machine and nothing else, have to work out together what caused
an outage. No single agent has enough evidence to answer alone.

The result we present is the failure case: cut the link between two agents,
re-run the same fault, and the system correctly declines to answer rather than
guessing.

University course project, 5 credits, team of four.
Intermediate report 2 is due 9 November. Demo day 23 November. Code freeze 20 November.

## The imaginary software

A Nordic online discussion platform. Pages: `/` (main, with forum search),
`/login`, `/signup`, `/{forum}` (100 messages per page).

| Component | Location | IP | Stack |
|---|---|---|---|
| App server 1 | Stockholm | 170.57.176.88 | Python, Ubuntu |
| App server 2 | Helsinki | 104.172.209.255 | Python, Ubuntu |
| User database | Vantaa | 88.211.152.184 | PostgreSQL |

Schemas: `users (id, username, password, email)`, `messages (id, message, username)`.

Scope was cut on 30 October. The message database in Uppsala and the Cloudinary
image service are OUT. Three components only.

## The design decision that everything rests on

Agents are split **by machine**, not by signal type. One agent per component.
Each agent connects to the telemetry database with its own Postgres role that
grants `SELECT` only on rows where `source` matches its machine. The isolation
is enforced by the database, not by convention.

If an agent could read another machine's rows, this stops being a multi-agent
project and becomes one program with three database connections.

## My role: platform owner (P1)

I build the platform that actually runs and emits its own telemetry. We are NOT
generating fake telemetry rows with a script. Real software runs, real faults
are injected with real commands, real telemetry comes out.

### What I own

- `otel.py` — the telemetry module every service imports
- `app/` — the app server (both instances run the same code, different env)
- `loadgen/` — fake visitors generating steady traffic

### What I do not own

- The telemetry schema and Postgres roles (P3)
- The agents (P4)
- The belief/merge logic (P2)

I never touch their files. If I need a change there, I ask them.

### The interface I expose

Telemetry rows landing in the telemetry database, every row tagged with the
`source` machine that produced it.

## How telemetry works

`otel.py` provides three things, and **no service ever writes telemetry rows any
other way**. The moment someone inserts directly in a handler, trace linkage
breaks and nobody notices until the agents cannot correlate anything.

1. `start_span(name, target, parent=None)` — context manager. On exit it writes
   a row with duration and status.
2. `log(severity, message, code=None)` — pulls the current `trace_id` from
   context, so every log line is linked to the request that produced it.
3. `metric(name, value, unit)` — counters on a timer.

Use a `contextvar` to hold the current trace and span id. That is what makes
`parent_id` work without threading it through every function call.

Writes go through a **background queue**, batched roughly every second. Never
write synchronously inside a request, or telemetry latency becomes app latency
and the measurements lie.

### Table shapes (OTLP-ish, owned by P3 but this is the agreed shape)

```
traces   span_id, trace_id, parent_id, ts, name, target, duration_ms, status, source, attrs
metrics  id, ts, metric, value, unit, source, attrs
logs     id, ts, severity, source, trace_id, message, code
```

`attrs` is JSONB for device type, username, forum id, and anything else that
turns up later without a schema change.

### In a handler

The HTTP request opens the parent span. Each SQL call opens a child span inside
it. Any error logs through `otel.log` so it inherits the trace id. Four or five
lines per endpoint.

## The two faults

Both are caused by **commands, not code paths**. Nobody can accuse us of writing
the answer into the data.

**Signup surge.** The load generator floods `/signup` while the user database is
configured with a low `max_connections`. The database starts refusing with
SQLSTATE 53300. The app servers see requests timing out at the SQL span, but
cannot see why. The database sees connection refusals, but cannot see the
traffic that caused them. Neither alone is the answer.

**Stockholm–Helsinki split.** `docker network disconnect` between the two app
servers. Each can still reach the database but not its peer. Cross-server spans
leave an unresolved `parent_id`. No database error appears anywhere, which is
what rules out the data tier.

**Rule for every fault:** the symptom must land on one machine and the cause on
a different one. A fault one agent can solve alone proves nothing and gets
rewritten or dropped.

## My deadlines

- **Wed 4 Nov** — one app server in a container, serving all four pages, backed
  by Postgres, emitting linked traces, metrics and logs.
- **Sat 7 Nov** — second app server, load generator producing steady baseline
  traffic.
- **Sun 8 Nov** — team integration run.
- **Mon 9 Nov** — report 2.

**Done when:** I can hit `/login` and see one parent span, one child span, one
log line and a metric row land in the telemetry database, all sharing a trace id.

## Team rules

- One owner per file. Ask before changing someone else's.
- Never block on another person's real code. Fake what you do not own.
- `docker compose up` must work on main at all times.
- All config from environment variables, defaults in the compose file. No
  hardcoded hostnames, ports, passwords or table names.
- Flag blockers the same day, not at the next meeting.
- Nothing new after 20 November. Broken things get cut, not fixed.

## Stack

Python 3.12, FastAPI, psycopg 3, PostgreSQL 16, Docker Compose. Everything local
and free. Ollama for the model later, but that is P4 and not before 9 November.
