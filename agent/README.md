# Agents (P4)

One node per machine: `stockholm`, `helsinki`, `vantaa`. A node reads only its own
machine's telemetry (its own DB role, row-level security does the rest) and gossips
its beliefs to the other two until they all hold the same state. Merging is P2's
`belief.py`.

For now every node runs `MockAgent` (`mock.py`): fixed claims per fault. With a DB it
picks the fault from its own rows (`53300` or failing `userdb` calls = surge,
`PEER_UNREACHABLE` = split) and cites real rows as evidence. Vantaa can't see a split,
so on a split or a quiet day it only says the database is fine. The real agent replaces
the mock in `make_agent()` in `node.py`, with the same two methods:

    analyse(shard)          -> [{"text", "confidence", "evidence_ids"}, ...]
    evaluate(claim, shard)  -> confidence for another node's claim, or None to abstain

`shard` is `{"traces": [...], "logs": [...], "metrics": [...]}`, rows as in the platform
README plus an `id` to cite: `span:<span_id>`, `log:<id>`, `metric:<id>`. It is `None`
when the node runs without a DB.

`evaluate` runs in the gossip thread, outside the node's lock, so a slow model doesn't
stall gossip. Each claim gets evaluated once per incident. The mock abstains where a
node can't see anything (vantaa on the link between the apps), and its numbers keep
the cause above the symptom: after a surge the database claim averages 0.83, the
failing signups 0.70.

## Run it

```sh
# with the platform, in Docker (agents on 9001-9003)
docker compose -f docker-compose.yml -f agent/compose.yml up -d --build
docker compose -f docker-compose.yml -f agent/compose.yml down -v   # both files, or the agents stay up

# or on your machine, no Docker; add "db" to read localhost:5433
pip install -r agent/requirements-dev.txt
agent/run_local.sh
agent/run_local.sh stop
```

## API

- `GET /health` returns `{node, mode, ok, incident, db}`. `db` is null when there is no DSN.
- `POST /incident` takes `{id, start?, end?, scenario?}`, reads the shard and adds the node's
  own claims. Default window is the last 60 s, times without a zone are UTC. A new `id`
  wipes the state. `scenario` (`surge`, `split`, `none`) forces the mock's answer, for
  running without a DB. Leave it out and the mock works it out from the rows; the reply
  says which one it went with.
- `POST /exchange` takes `{incident, from, state, window?}`, merges it and answers
  `{node, incident, state}`. 409 if the node is on another incident, 400 for a broken
  state (ours stays as it was). A node with no incident that gets a `window`
  (`{start, end, scenario}`, gossip always sends it) joins that incident first, which is
  how a restarted node gets back in without anyone calling `/incident` again.
- `GET /state` returns `{node, incident, digest, state, peers}`. `peers` maps each peer to the
  unix time it was last heard from in this incident.

```sh
for p in 9001 9002 9003; do
  curl -s localhost:$p/incident -H 'content-type: application/json' -d '{"id": "inc-1", "scenario": "surge"}'
done
sleep 2; for p in 9001 9002 9003; do curl -s localhost:$p/state | jq -r .digest; done  # same three times
```

## Config

`NODE_ID` (required), `PEERS` (`name=url,...`), `TELEMETRY_DSN` (always an `agent_<node>`
role, a node that can see other machines' rows refuses to work), `AGENT_MODE=mock`,
`GOSSIP_SECONDS=1` (0 = never push), `PEER_TIMEOUT=2`, `SHARD_LIMIT=5000`,
`INCIDENT_WINDOW_SECONDS=60`.

## Checks

```sh
agent/check.sh          # pytest + three local nodes converge on surge, split, none (~10 s)
agent/check.sh stack    # platform + agents in Docker, platform ./check.sh, isolation, gossip, restart
agent/check.sh faults   # real surge and split with no hint, claims must cite real rows (~90 s)
agent/check.sh all      # all of it, then compose down -v
```

Last line is `PASS` or `FAIL`. Run `all` before pushing.

## Not done yet

- Agents share one network, so the "cut one agent link" demo still needs its own networks.
- Cause and symptom are only told apart by confidence. A `kind` on the claim itself
  would need a field in `belief.py`, that's for P2 to decide.
- No real model yet (Ollama, after 9 November).
