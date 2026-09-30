# Week 1: foundations

Nothing useful gets built this week. The goal is that by Friday, all four of us
can work in parallel without waiting on each other. If we skip this, P2 spends
week 2 blocked on P1 and P3 spends it blocked on P4, and we lose a third of the
project to waiting.

**Definition of done:** `docker compose up` runs on all four laptops, the four
stubs call each other successfully, and the fake agent is committed to main.

---

## Everyone installs, Monday

- Python 3.12
- Docker Desktop (free for students)
- Git

Nothing else this week. Redis clients, etcd clients, Chart.js and Toxiproxy
land in weeks 2 and 5, so don't install them now.

---

## The four interfaces

Agree these Monday, before anyone writes real code. Once they're fixed, each of
us builds behind our own interface and fakes everyone else's.

    get_shard(incident, window)      -> list of records        P1
    merge(local, remote)             -> merged state           P2
    current_leader()                 -> node id                P3
    analyse(shard)                   -> list of hypotheses     P4

A hypothesis is:

    {"text": "<one sentence claim>",
     "confidence": 0.0-1.0,
     "evidence_ids": ["pg-150", "dep-1"]}

Don't change these without telling everyone. If one has to change, it's a
group decision, not a commit.

---

## P1: storage

Build the fake telemetry generator. Normal activity for 30 minutes, then one
thing goes wrong. Because we wrote the fault, we know the right answer, which
is what makes accuracy measurable later.

- Six services: frontend, checkout, payment, postgres, deploys, traces
- Every record needs a unique `id`, since agents cite these as evidence
- One fault to start: a config change raises a database connection limit too
  high, the pool fills, checkout breaks
- Ship `get_shard()` returning one service's records

Stack: Python only. `numpy` if you want realistic noise.

**Done when:** `generate("pool_exhaustion")` returns six chunks, and the fault
is invisible in any single chunk on its own.

---

## P2: coordination

Build the shape of the shared state, with the merge logic left empty. The real
merge is week 2 and it's the hardest thing in the project, so spend this week
on structure and on getting the test setup working.

- A state object holding hypotheses, evidence and per-agent confidence scores
- `merge(local, remote)` that currently just returns local
- A `/gossip` endpoint that accepts a state and returns ours
- Install `pytest` and `hypothesis` now and get one trivial test running

Stack: FastAPI, uvicorn, httpx, pytest, hypothesis.

**Done when:** two processes can POST states to each other without crashing.

---

## P3: infrastructure

Build the thing everyone else runs inside.

- Repo, branch protection, a README anyone can follow
- Dockerfile for a single agent
- Compose file starting six identical containers, each with a different
  `NODE_ID` and `SHARD` environment variable
- `current_leader()` returning a hardcoded `"s0"` for now
- Pull the `redis` and `etcd` images this week so nobody discovers a download
  problem in week 2

Stack: Docker Compose.

**Done when:** `docker compose up` gives six containers that start cleanly on
all four machines.

---

## P4: agents

Build the fake agent. This is the most important deliverable of week 1, because
everyone else develops against it for the next three weeks.

- A class with `analyse(shard)` and `evaluate(claim, shard)` returning fixed,
  hardcoded answers, no model involved
- One fixed hypothesis per service, matching P1's fault
- Same method signatures the real model will use later, so swapping it in is a
  one-line change

Also start the 5GB `ollama pull qwen2.5:7b` download this week and confirm it
runs on the weakest laptop in the group. If it doesn't, we need to know now, not
in week 4.

Stack: Python, Ollama installed but not yet used.

**Done when:** the fake agent is on main and the other three are importing it.

---

## Friday

Fifteen minutes, all four of us:

1. Everyone pulls main and runs `docker compose up`
2. Walk one incident end to end (fake data in, fake hypotheses out)
3. Confirm nobody is blocked on anybody for week 2
4. P2 confirms the convergence test setup runs, since that's next week's gate

---

## Week 2 preview

- P1: shard placement across nodes, three copies each
- P2: the real merge, plus the test proving agents converge regardless of
  message order. This is the single most important gate in the project.
- P3: task queue and real leader election
- P4: real prompts, one agent reading one chunk
