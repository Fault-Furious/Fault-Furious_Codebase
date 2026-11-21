# Agents (P4)

One node per machine: `stockholm`, `helsinki`, `vantaa`. A node reads only its own
machine's telemetry (its own DB role, row-level security does the rest) and gossips
its beliefs to the other two until they all hold the same state. Merging is P2's
`belief.py`.

There are two agents behind a node, picked with `AGENT_MODE`, both with the same two methods:

    analyse(shard)          -> [{"text", "confidence", "evidence_ids"}, ...]
    evaluate(claim, shard)  -> confidence for another node's claim, or None to abstain

`mock` (default, `mock.py`): fixed claims per fault. With a DB it picks the fault from
its own rows (`53300` or failing `userdb` calls = surge, `PEER_UNREACHABLE` = split) and
cites real rows as evidence. Vantaa can't see a split, so on a split or a quiet day it
only says the database is fine. The rest of the team builds against this one.

`llm` (`llm.py`): a local model through Ollama, `qwen3:8b` by default. The node's rows
are grouped and counted into a short summary (one line per error, span kind or metric,
with a few ids each), and the model picks claims from a fixed list (`CLAIMS`) with a
verdict (supported, contradicted, cannot see), how sure it is of it, and ids from that
summary. Fixed texts because `belief.py` merges claims by a hash of the text. A JSON
schema keeps it to those texts and those ids. Only supported claims it is at least half
sure of become its own. For someone else's claim it first says whether its rows touch
it at all, if not it abstains. `score()` turns verdict and certainty into the confidence
that goes into the belief state: supported 0.55 to 1, contradicted 0.45 to 0, never 0.5.
The list holds the mock's four claims plus a few more so it has to choose: all apps fine
(right only on a quiet day), slow queries, and an app server down. Nothing can back the
last two: a machine knows it is up itself, and the others only see that their calls fail.

`shard` is `{"traces": [...], "logs": [...], "metrics": [...]}`, rows as in the platform
README plus an `id` to cite: `span:<span_id>`, `log:<id>`, `metric:<id>`. It is `None`
when the node runs without a DB.

`evaluate` runs in its own thread, outside the node's lock, so a slow or hanging model
doesn't stall gossip. Each claim gets evaluated once per incident. The mock abstains where a
node can't see anything (vantaa on the link between the apps), and its numbers keep
the cause above the symptom: after a surge the database claim averages 0.83, the
failing signups 0.70.

## Run it

```sh
# whole platform and agents, in Docker (agents on 9001-9003)
docker compose up -d --build
docker compose down -v

# or on your machine, no Docker; add "db" to read localhost:5433
pip install -r agent/requirements-dev.txt
agent/run_local.sh
agent/run_local.sh stop

# with the model instead of the mock, Ollama on the host
ollama pull qwen3:8b
docker compose -f docker-compose.yml -f agent/compose.yml up -d --build
AGENT_MODE=llm agent/run_local.sh db   # or locally, the model needs rows to read
```

### When the model is down

The node retries once, then makes no claims of its own: `/incident` returns an `error`,
`/health` says `"model": false`, and `/state` keeps the claims it still owes an opinion on
in `pending`. It goes on gossiping, so the other two still agree with each other. Every
`MODEL_RETRY_SECONDS` it asks again, and once the model answers it adds its own claims
and opinions like nothing happened. It never falls back to the mock.

## API

- `GET /health` returns `{node, mode, ok, incident, db, model}`. `db` is null when there is
  no DSN, `model` is null for the mock, else whether Ollama answers and has the model.
- `POST /incident` takes `{id, start?, end?, scenario?}`, reads the shard and adds the node's
  own claims. Default window is the last 60 s, times without a zone are UTC. A new `id`
  wipes the state. `scenario` (`surge`, `split`, `none`) forces the mock's answer, for
  running without a DB. Leave it out and the mock works it out from the rows; the reply
  says which one it went with. The model ignores `scenario`. Its claims carry a `reason`,
  and `error` is set when it couldn't be asked.
- `POST /exchange` takes `{incident, from, state, window?}`, merges it and answers
  `{node, incident, state}`. 409 if the node is on another incident, 400 for a broken
  state (ours stays as it was). A node with no incident that gets a `window`
  (`{start, end, scenario}`, gossip always sends it) joins that incident first, which is
  how a restarted node gets back in without anyone calling `/incident` again. While it is
  still joining, more gossip for it gets a 409 instead of a second analysis.
- `GET /state` returns `{node, incident, digest, state, peers, pending, error}`. `peers` maps
  each peer to the unix time it was last heard from in this incident. `pending` is above 0
  while the node still owes work (claims to score, its own analysis if the model failed),
  `error` is the last model error.

```sh
for p in 9001 9002 9003; do
  curl -s localhost:$p/incident -H 'content-type: application/json' -d '{"id": "inc-1", "scenario": "surge"}'
done
sleep 2; for p in 9001 9002 9003; do curl -s localhost:$p/state | jq -r .digest; done  # same three times
```

## Config

`NODE_ID` (required), `PEERS` (`name=url,...`), `TELEMETRY_DSN` (always an `agent_<node>`
role, a node that can see other machines' rows refuses to work), `AGENT_MODE=mock`,
`GOSSIP_SECONDS=1` (0 = never push and never score other nodes' claims), `PEER_TIMEOUT=2`, `SHARD_LIMIT=5000`,
`INCIDENT_WINDOW_SECONDS=60`. Model mode: `OLLAMA_URL=http://localhost:11434`,
`OLLAMA_MODEL=qwen3:8b`, `LLM_TIMEOUT=120` (three nodes queue on one Ollama),
`LLM_RETRIES=1` (only for no answer at all, a broken answer is an abstain),
`MODEL_RETRY_SECONDS=5`.

## Checks

```sh
agent/check.sh          # pytest + three local nodes converge on surge, split, none (~10 s)
agent/check.sh stack    # platform + agents in Docker, platform ./check.sh, isolation, gossip, restart
agent/check.sh faults   # real surge and split with no hint, claims must cite real rows (~90 s)
agent/check.sh all      # all of it, then compose down -v
agent/check.sh llm      # model agents on real faults, a node dying, cut off, model down (~20 min)

LLM_TESTS=1 .venv/bin/python -m pytest -q tests/test_llm.py   # model on captured rows (~10 min)
```

Last line is `PASS` or `FAIL`. Run `all` before pushing, `llm` too if you touched `llm.py`.

`llm` judges the swarm, not exact claims. Each machine has to back what it can see
(vantaa the refused connections and the apps the failing signups on a surge, the apps
the cut link and vantaa the healthy db on a split, vantaa the healthy db on a quiet day),
and nothing wrong for that fault may end up with a mean of 0.5 or more. A node that is
dead or cut off is left out of that, and the other two must not have heard from it for
5 s. One whose model we took away (`--down`) still gossips, it just isn't asked to back
anything. `tests/data` holds rows captured from a real
surge, split and quiet minute, thinned out, for the model tests.

## Not done yet

- Agents gossip over `nordic-agent-peer` but also share `agent_db`, so taking an agent off
  `nordic-agent-peer` alone cuts nothing. `check.sh llm` cuts it off from both. Cutting just
  one link (stockholm to helsinki) needs each agent on its own DB network, that's in
  `docker-compose.yml`.
- Cause and symptom are only told apart by confidence. A `kind` on the claim itself
  would need a field in `belief.py`, that's for P2 to decide.
- `qwen3:8b` gets these faults right, but it is a small model and the prompt had to spell
  some things out: claims in the units of the summary ("over 1000 ms", not "a second"),
  "your own machine is up", "a call that can't connect doesn't mean the other side is
  down", and a plain yes/no on whether its rows touch a claim before it judges it.
  Without those it scored claims about machines it can't see. Vantaa still sometimes
  backs "signup inserts are failing" from the 23505 rows. `qwen3:32b` (`OLLAMA_MODEL`)
  needed none of that in a test run, about 4x slower.
- The model sees the same three captured faults the prompt was tuned on (`tests/data`).
  `check.sh llm` runs fresh faults, so that is the honest test.
