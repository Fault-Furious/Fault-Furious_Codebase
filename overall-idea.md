# Fault & Furious: Distributed Incident Triage Swarm

**DataStorm**

**Group members:**\
\[Add names\]

**Workshop #:**\
\[Add workshop number\]

------------------------------------------------------------------------

## MOTIVATION

**Target group:** Software engineers, DevOps engineers, SREs, and
on-call engineers responsible for diagnosing production incidents in
distributed systems.

**End-user:** Engineers who need to identify the root cause of a
production outage from fragmented telemetry while the system may itself
be experiencing failures.

When a production system fails, the evidence required to diagnose the
incident is rarely located in one place. Logs may exist on one set of
machines, metrics in another monitoring system, traces somewhere else,
and deployment or configuration history in separate systems.

An on-call engineer must manually connect these signals under time
pressure.

The main problems we aim to address are:

-   **Fragmented evidence** --- logs, metrics, traces, deployments and
    configuration changes are distributed across different sources.
-   **Partial visibility** --- no single machine or agent necessarily
    has the complete picture.
-   **Manual correlation** --- engineers have to search, compare and
    connect evidence across systems.
-   **Centralization bottlenecks** --- sending all telemetry to one
    centralized reasoning system can create a context and availability
    bottleneck.
-   **Failure during investigation** --- the infrastructure used for
    diagnosis may itself experience node failures or network partitions.
-   **Conflicting hypotheses** --- different parts of the system may
    produce different explanations for the same incident.
-   **Coordination cost** --- adding more agents improves parallelism
    but also increases communication and inference cost.

The central problem is therefore:

> **How can a set of agents, each holding only a partial and possibly
> stale view of system state, converge on a useful diagnosis while nodes
> fail and the network splits?**

------------------------------------------------------------------------

## CORE IDEA

### From one large agent to a distributed investigation swarm

**Fault & Furious** is a distributed incident-triage system in which
multiple agents investigate different partitions of production telemetry
and exchange their findings until they converge on a shared diagnosis.

Instead of sending all telemetry to one large language model, the system
deliberately gives each shard agent only a partial view of the evidence.

This forces the agents to perform meaningful coordination rather than
decorative parallelism.

The overall pipeline is:

``` text
Synthetic production telemetry
        ↓
Partition by service + time window
        ↓
Consistent-hash placement
        ↓
Three-way replication
        ↓
Shard agents investigate local evidence
        ↓
Agents exchange hypotheses through gossip
        ↓
CRDT merges hypotheses + evidence + confidence
        ↓
Cross-check and confidence updates
        ↓
Swarm converges
        ↓
Raft-elected leader
        ↓
Final incident report
```

The central idea is:

> **No agent sees the whole picture. The swarm builds it together.**

------------------------------------------------------------------------

## WHAT MAKES IT A MULTI-AGENT PROBLEM?

The project is not simply several LLM calls running in parallel.

Each agent has:

-   A **partial view** of the telemetry.
-   Its own locally generated hypotheses.
-   Its own evidence and confidence estimates.
-   The ability to exchange state with other agents.
-   The ability to evaluate another agent's hypothesis against its own
    local evidence.

This creates an actual coordination problem.

For example:

``` text
Agent A:
"Latency has increased."

Agent B:
"Database connections are exhausted."

Agent C:
"A deployment changed the connection-pool configuration."

Agent D:
"The deployment occurred immediately before the spike."
```

No individual agent necessarily has enough information to explain the
incident.

After communication and cross-checking, the swarm may converge on:

``` text
Deployment
     ↓
Connection-pool exhaustion
     ↓
Database saturation
     ↓
Latency spike
```

------------------------------------------------------------------------

# SYSTEM ARCHITECTURE

## 1. Data Layer

Production-like telemetry is generated synthetically and divided across
multiple nodes.

### Telemetry

The system works with:

-   Logs
-   Metrics
-   Traces
-   Deployment history
-   Configuration changes

Telemetry is partitioned primarily by:

-   Service
-   Time window

### Consistent hashing

A consistent-hash ring determines where each shard is placed.

When a node joins or leaves, only approximately `1/N` of the data needs
to move instead of reshuffling the entire dataset.

### Replication

Each shard is replicated across **three nodes**.

Writes use quorum acknowledgement so that the system does not depend on
a single copy of a shard.

``` text
             SHARD A
                │
        ┌───────┼───────┐
        ↓       ↓       ↓
      Node 1  Node 2  Node 3
      replica replica replica
```

------------------------------------------------------------------------

## 2. Agent Layer

### Shard agents

Each telemetry partition has an associated shard agent.

A shard agent:

1.  Queries only its local partition.
2.  Summarizes the evidence available to it.
3.  Generates structured hypotheses.
4.  Assigns confidence and evidence IDs.
5.  Shares its belief state with peers.
6.  Evaluates hypotheses received from other agents.

The intentionally limited local view is important. If every agent could
access everything, there would be little reason for distributed
coordination.

### Specialist agents

Specialist agents can optionally provide additional domain-specific
evidence.

Examples:

-   Deployment-history agent
-   Configuration-change agent
-   Infrastructure agent

### Coordinator

The final report is generated by a **Raft-elected leader** rather than a
permanently fixed coordinator.

Any eligible shard agent can become leader.

------------------------------------------------------------------------

# DISTRIBUTED-SYSTEM MECHANISMS

## 1. Sharding

**Purpose:** Distribute telemetry.

Telemetry is partitioned across nodes using consistent hashing.

> **Split the evidence.**

## 2. Replication

**Purpose:** Preserve availability of evidence.

Each shard is stored on three replicas with quorum acknowledgement.

> **Keep the evidence alive.**

## 3. Gossip

**Purpose:** Exchange local knowledge without a central communication
hub.

Each agent periodically selects a small number of random peers and
exchanges belief state.

The planned default gossip fanout is **three peers**.

> **Share what you know.**

## 4. CRDT-based belief state

**Purpose:** Merge concurrent agent beliefs.

The shared state contains:

-   Hypotheses
-   Supporting evidence
-   Confidence scores

The proposed representation uses:

-   An OR-Set for hypotheses.
-   A last-write-wins map for confidence scores.

Agents can continue making local updates during temporary communication
failures and reconcile those updates later.

> **Merge what everyone knows.**

## 5. Raft

**Purpose:** Elect the agent responsible for producing the final
incident report.

The leader is elected dynamically rather than permanently assigned.

> **Elect the reporter.**

## 6. Failure detection

Agents monitor peer heartbeats using phi-accrual failure detection.

When a peer appears unavailable, surviving agents can continue working
with the remaining cluster.

The system therefore aims to tolerate **partial failure**, not complete
infrastructure loss.

> **A failed node does not have to end the investigation.**

------------------------------------------------------------------------

# WHERE AI IS USED

A major design principle of Fault & Furious is:

> **The model handles semantic reasoning; deterministic distributed
> mechanisms handle coordination.**

## Model responsibilities

### Local hypothesis generation

An agent summarizes its local shard and produces structured output
containing a hypothesis, confidence and evidence IDs.

### Evidence evaluation

When an agent receives another agent's hypothesis, the model checks it
against its local evidence and returns a confidence adjustment.

### Hypothesis deduplication

The model determines whether differently worded hypotheses represent the
same underlying claim.

For example:

``` text
"Connection pool exhausted"

"Too many open connections to Postgres"
```

may represent the same hypothesis.

### Final report synthesis

The elected leader converts the merged belief state into a readable
incident timeline and final report.

------------------------------------------------------------------------

# WHERE AI IS NOT USED

The following components remain deterministic:

  Responsibility      Mechanism
  ------------------- -----------------------------
  Shard placement     Consistent hashing
  Leader election     Raft
  Failure detection   Phi-accrual over heartbeats
  Peer exchange       Epidemic gossip
  State merge         CRDT
  Anomaly trigger     Z-score / EWMA
  Replication         Quorum writes
  Task delivery       Durable queue
  Idempotency         Idempotency keys

A deterministic mock model can therefore be used during development and
consensus testing.

------------------------------------------------------------------------

# IMPORTANT DESIGN CHALLENGE: NON-DETERMINISTIC AI

Traditional distributed systems often assume that retrying an operation
produces the same result. LLM inference does not necessarily behave this
way.

For example, two agents could ask whether:

``` text
"connection pool exhausted"
```

and

``` text
"too many open Postgres connections"
```

are the same claim and receive different answers.

However, CRDT convergence requires deterministic state merging.

### Proposed solution

Cache semantic deduplication decisions using a hash of the pair of claim
texts.

``` text
Claim A + Claim B
        ↓
     hash(...)
        ↓
deduplication cache
        ↓
canonical decision
```

The first decision becomes the decision reused by the rest of the swarm.

An alternative is to perform semantic deduplication only on the elected
leader, trading availability for stronger determinism.

------------------------------------------------------------------------

# INVESTIGATION WORKFLOW

## 1. Ingest

Synthetic telemetry enters the system.

The hash ring assigns records to shards and replicas acknowledge the
writes.

## 2. Trigger

A deterministic anomaly detector detects an abnormal metric pattern.

Possible detectors include Z-score and EWMA.

An incident ID is created and an investigation task is placed on the
durable queue.

## 3. Fan out

Each shard agent receives the investigation task, queries only its local
partition, and generates hypotheses.

## 4. Gossip

Agents periodically exchange belief states with random peers.

Because state merging is order-independent, agents do not need to
communicate with a specific central node.

## 5. Cross-check

An agent receives another agent's hypothesis and checks it against its
own evidence.

## 6. Converge

The shared belief state stabilizes. Once a hypothesis crosses the
confidence threshold and the belief state stops changing sufficiently,
the elected leader produces the final report.

## 7. Break it

The system is deliberately attacked during the investigation:

-   Kill a node.
-   Add network latency.
-   Drop messages.
-   Partition the cluster.
-   Reconnect the partition.
-   Kill a node while hypotheses are being exchanged.

------------------------------------------------------------------------

# FAILURE MODEL

The project focuses on **partial failure**.

## Node failure

``` text
A ─ B ─ C ─ D ─ E

        C dies

A ─ B    X    D ─ E
```

The failed agent does not continue computing. The remaining agents
continue using replicated evidence, their local observations, existing
belief state, gossip, and dynamic leadership where required.

## Network partition

``` text
A ─ B ─ C       D ─ E ─ F
```

The two groups may temporarily have different belief states.

When connectivity returns:

``` text
A ─ B ─ C ═══ D ─ E ─ F
             ↓
        CRDT MERGE
             ↓
         CONVERGENCE
```

## Complete infrastructure failure

Fault & Furious does **not** claim to continue operating if every node
hosting the swarm is unavailable.

The intended resilience property is:

> **Partial infrastructure failure should not necessarily terminate the
> investigation.**

A complete loss of all compute nodes would require an external surviving
cluster or infrastructure region.

------------------------------------------------------------------------

# EXPERIMENTAL EVALUATION

The project compares:

### Baseline A --- Centralized

``` text
All available telemetry
        ↓
Single reasoning agent
        ↓
Diagnosis
```

### Baseline B --- Fault & Furious

``` text
Partitioned telemetry
        ↓
Multiple shard agents
        ↓
Gossip + CRDT
        ↓
Converged belief
        ↓
Raft leader
        ↓
Diagnosis
```

The same controlled incidents should be evaluated against both
approaches.

## Metrics

  -----------------------------------------------------------------------
  Measure                 Varied against          What it shows
  ----------------------- ----------------------- -----------------------
  Time to convergence     Shard count / agent     Whether parallelism
                          count                   pays

  Diagnosis accuracy      Injected fault type     Swarm vs centralized
                                                  baseline

  Accuracy under          Partition size and      Cost of choosing
  partition               duration                availability

  Messages per            Gossip fanout           Coordination overhead
  investigation                                   

  Inference cost per      Agent count             Cost curve
  incident                                        

  Recovery time           Nodes killed mid-run    Graceful degradation
  -----------------------------------------------------------------------

------------------------------------------------------------------------

# GROUND TRUTH

A fault injector deliberately introduces known incidents into the
synthetic environment.

For example:

``` text
Fault injected:
Connection pool size reduced

Expected causal chain:
Pool exhaustion
        ↓
Database saturation
        ↓
Latency increase
```

Because the fault injector knows what it changed, the system can compare
its diagnosis against a known root cause.

This makes diagnosis accuracy measurable rather than subjective.

------------------------------------------------------------------------

# TECHNOLOGY STACK

-   **Language:** Python + asyncio
-   **Queue:** Redis Streams, or Kafka if consumer groups and replay
    become important
-   **Consensus:** etcd, or a small hand-rolled Raft implementation if
    schedule permits
-   **Gossip + CRDT:** implemented by the project team
-   **Deployment:** Docker Compose, initially 5--10 nodes on one machine
-   **Chaos:** Toxiproxy or iptables for latency, drops and partitions
-   **Model:** Hosted LLM behind a thin interface
-   **Testing:** Deterministic mock model plus property/unit tests

------------------------------------------------------------------------

# PROJECT TIMELINE

## WEEK / MILESTONE 1 --- SHARDED STORE

-   Ingest synthetic telemetry.
-   Implement consistent hashing.
-   Implement three-way replication.
-   Implement quorum acknowledgement.
-   Test node join/leave and rebalancing.

**Deliverable:** Working distributed telemetry store.

## MILESTONE 2 --- ONE AGENT, ONE SHARD

-   Define prompt contract.
-   Define structured hypothesis schema.
-   Implement local analysis.
-   Establish deterministic mock model.

**Deliverable:** End-to-end local root-cause analysis.

## MILESTONE 3 --- GOSSIP + MERGE

-   Implement gossip.
-   Implement belief-state exchange.
-   Implement CRDT merge.
-   Add convergence/property tests.

**Deliverable:** Multiple agents exchanging and merging hypotheses.

## MILESTONE 4 --- ELECTION + SYNTHESIS

-   Implement/integrate Raft.
-   Implement leader election.
-   Implement convergence threshold.
-   Generate final report.

**Deliverable:** Full multi-agent diagnosis pipeline.

## MILESTONE 5 --- CHAOS + MEASUREMENT

-   Build fault injector.
-   Run centralized baseline.
-   Run distributed swarm.
-   Inject node failures and network partitions.
-   Collect evaluation metrics.
-   Analyze results.
-   Prepare report and demo.

**Deliverable:** Reproducible experiments and live demonstration.

------------------------------------------------------------------------

# TEAM ORGANIZATION

  --------------------------------------------------------------------------------
  Owner                   Scope                   Interface
  ----------------------- ----------------------- --------------------------------
  **Storage**             Hash ring, replication, `get_shard(incident, window)`
                          rebalancing, query API  

  **Messaging**           Gossip, failure         `merge(local, remote)`
                          detection, CRDT         

  **Consensus**           Raft, durable queue,    `current_leader()`
                          idempotency             

  **Agent Layer**         Prompting, structured   `analyse(shard) -> hypotheses`
                          output, deduplication,  
                          report                  

  **Everyone**            Fault injection,        ---
                          experiments,            
                          integration and final   
                          report                  
  --------------------------------------------------------------------------------

Interfaces should be agreed early so each technical track can progress
independently using mocks.

------------------------------------------------------------------------

# RISKS AND MITIGATIONS

  -----------------------------------------------------------------------
  Risk                                Mitigation
  ----------------------------------- -----------------------------------
  Raft implementation takes too long  Start with etcd; implement custom
                                      Raft only if schedule permits

  LLM inference becomes expensive     Deterministic mock during
                                      development

  Synthetic incidents are too easy    Include correlated and cascading
                                      faults

  LLM results are not reproducible    Fixed seeds where supported,
                                      temperature zero where supported,
                                      cached responses

  Semantic deduplication breaks       Cache deduplication decisions
  convergence                         

  Network partitions reduce accuracy  Measure the availability/accuracy
                                      trade-off

  More agents increase cost           Measure inference and message
                                      overhead

  Swarm performs worse than           Treat the crossover point as a
  centralized baseline                valid result
  -----------------------------------------------------------------------

------------------------------------------------------------------------

# SUCCESS CRITERIA

## Functional

-   Telemetry is partitioned and replicated.
-   Agents query local evidence.
-   Agents generate structured hypotheses.
-   Agents exchange hypotheses through gossip.
-   Belief states merge correctly.
-   A leader can be elected.
-   A final incident report can be generated.
-   Nodes can be killed during investigation.
-   Network partitions can be injected and recovered from.

## Experimental

Produce measurable results for:

-   Diagnosis accuracy.
-   Time to convergence.
-   Accuracy under partition.
-   Message overhead.
-   Inference cost.
-   Recovery time.

## Demonstration

``` text
OPEN INCIDENT
      ↓
AGENTS INVESTIGATE
      ↓
HYPOTHESES EXCHANGE
      ↓
KILL NODE
      ↓
PARTITION NETWORK
      ↓
RECONNECT
      ↓
CRDT RECONCILIATION
      ↓
CONVERGENCE
      ↓
FINAL INCIDENT REPORT
```

------------------------------------------------------------------------

# FINAL DELIVERABLES

1.  Running 5--10 node cluster using Docker Compose.
2.  Synthetic telemetry generator.
3.  Labelled fault injector with known ground truth.
4.  Distributed telemetry store with sharding, replication and
    rebalancing.
5.  Multi-agent investigation layer.
6.  Failure detection and dynamic leadership.
7.  Centralized-vs-distributed evaluation framework.
8.  Experimental results across the six primary measures.
9.  Written report covering semantic deduplication, partition trade-offs
    and coordination cost.
10. Live demo showing an incident being investigated while the swarm
    experiences disruption.

------------------------------------------------------------------------

# PROJECT PHILOSOPHY

Fault & Furious is not based on the assumption that:

> **More agents = better AI.**

Instead, the project asks:

> **Can distributed coordination make agentic incident diagnosis more
> resilient, and when is that resilience worth the coordination cost?**

The swarm may outperform the centralized baseline. It may perform
similarly. It may perform worse once coordination overhead becomes
dominant.

All of these outcomes can be useful.

The goal is to **measure the boundary between distributed resilience and
distributed overhead**.

------------------------------------------------------------------------

# SHORT PROJECT DESCRIPTION

### One sentence

> **Fault & Furious is a distributed multi-agent incident-triage system
> that combines partial telemetry views, gossip, CRDT-based belief
> merging and dynamic leader election to diagnose production failures
> under node and network failures.**

### Short pitch

> **Production incidents scatter evidence across machines and services.
> Fault & Furious gives different agents different pieces of that
> evidence, lets them exchange and reconcile their hypotheses, and tests
> whether they can still converge on a diagnosis when nodes fail or the
> network partitions.**

### The question

> **When production breaks, can a swarm of partially informed agents
> figure out why --- even when the swarm itself starts breaking?**
