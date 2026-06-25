# Polyp

*A general-purpose state machine for autonomous optimization loops — four LLM agents over a Postgres queue, drives any try-evaluate-iterate problem.*

![architecture](docs/architecture.svg)

## What this is

Polyp is a Postgres-backed state machine that drives any **try → evaluate → propose-next → repeat** loop. An **Architect** drafts a candidate (a `run.spec` + code), a **Worker** evaluates it, an **Analyzer** files a verdict, and an **Auto-suggester** proposes the next candidate based on what just happened. The cycle repeats until you stop it (or until the auto-suggester stops finding anything worth trying).

Polyp is the *coordinator*, not the *worker*. The worker is yours — any script that takes a spec and writes a `results.json`. Hardware is whatever your worker needs: a single CPU, a GPU box, a distributed cluster, an API call.

Fits anything with a try-evaluate-iterate shape: hyperparameter sweeps, LoRA / full-FT runs, kernel ablations, prompt-engineering loops, retrieval-recipe tuning, scheduler or RL tuning, anything where the next experiment depends on the last one's result.

See it in action: [a 108-experiment LoRA fine-tuning run on gpt-oss-20b that lifted HotpotQA accuracy by 59pp](https://coralbricks.ai/research/lora-trajectory).

## Prerequisites

Three things, all yours:

| | What | Notes |
|---|---|---|
| **Coding agent** | Claude Code (canonical) or any agent that can read a task spec and write a worker script. | The Architect / Analyzer / Auto-suggester are headless agent invocations. Wire whatever you want; the runner scripts call `claude` by default. |
| **Postgres database** | Any Postgres ≥ 13. Local for testing, RDS / Cloud SQL / Neon / Supabase for production. | Schema lives in `queue/schema.sql`. |
| **Hardware** | Whatever your workload needs — a single CPU, a GPU box, a distributed cluster, an API call. | The Architect drafts both the `run.spec` and the worker code per task; you bring the iron. |

You don't have to bring a worker script. The Architect generates the spec + the code per task. In practice, giving the agent some helper libs + a `CLAUDE.md` style template makes its drafts converge faster for non-trivial workloads — but that's onboarding quality, not a prereq.

The toy example in `examples/toy_sweep/` ships with a **stubbed worker** so you can exercise the full loop on a laptop with no GPU — but the stub doesn't do real work. Swap it for your own evaluator (or let the Architect draft one) to drive actual optimization.

## Quick start

### 1. Stand up a Postgres database

```bash
export CB_QUEUE_DB_URL="postgres://user:pass@host:5432/dbname"
```

Or write the URL to `~/.cb_queue_db_url`, or set `CB_QUEUE_DB_SECRET` to a Secrets Manager name (`cbq` will fetch it).

### 2. Install + initialize

```bash
pip install -e .
cbq init-db          # applies queue/schema.sql to the cb_queue Postgres schema
cbq status           # sanity check
```

### 3. Submit your first task

The toy example is a stubbed LR sweep — the worker doesn't train anything, it just emits a deterministic unimodal accuracy curve so the auto-suggester has a real surface to climb.

```bash
cd examples/toy_sweep
cbq submit spec.yaml --slug hello-world --kind research
cbq list --status enqueued
```

**Next step:** replace `examples/toy_sweep/run.py` with your own evaluator to run real work.

### 4. Run the loop

You can run the agents however you like — systemd units in `runner/`, tmux watchers, cron, Kubernetes. The pattern is:

```bash
# Architect side (orchestrator box)
runner/architect_watcher.sh           # claims enqueued → coding → ready

# Worker side (your hardware)
runner/worker_watcher.sh              # claims ready → executing → executed

# Architect side again
runner/architect_watcher.sh           # claims executed → analyzing → done/falsified/blocked
                                       # then runs suggest_experiment.sh → enqueues next
```

## How the loop runs

```
enqueued → coding → ready → executing → executed → analyzing
analyzing → done | falsified | blocked   ← verdicts
coding    → code_failed                  ← parked: in-phase retries exhausted
analyzing → executed | analyze_stuck     ← one auto-retry, then parked
enqueued  → cancelled                    ← human veto window
```

The `experiments` table is **authoritative for live state**. Git holds only the archive exports (`done/`, `falsified/`) and your per-experiment `exp/NNNN-*` branches.

| Phase | Who | What |
|---|---|---|
| `coding` | Architect | Drafts the `run.spec` + worker code from the task description. |
| `executing` | Worker | Claims a `ready` task, runs it on your hardware, writes results. |
| `analyzing` | Analyzer | Reads `results.json` + your scoring logic, files the verdict. |
| (post-verdict) | Auto-suggester | Proposes the next candidate based on the verdict. Back to `enqueued`. |

Humans are the **overseer**, not the operator: review the queue, cancel runs that are wasting compute, file new constraints when something subtle breaks.

## Layout

```
polyp/
├── queue/                 # cbq CLI + Postgres schema + telemetry publishers
│   ├── cbq.py             # the state-machine CLI (1.5k LOC, no magic)
│   ├── schema.sql         # cb_queue.experiments + leases + workers
│   └── publish_*.py       # CloudWatch snapshot uploaders (optional)
├── runner/                # one-line orchestration around cbq
│   ├── architect_*.sh     # code phase, analyze phase, watcher loop
│   ├── worker_*.sh        # claim, execute, reconcile, shift handoff
│   ├── suggest_experiment.sh
│   ├── *.service          # systemd units
│   └── finalize_completed.sh
├── lib/                   # generic helpers: GPU detect, HF hub, progress logging
├── examples/toy_sweep/    # minimal end-to-end loop (stubbed worker)
└── docs/                  # architecture diagram
```

## Operational tips

### The constraints pattern

Every novel failure — a kernel bug, an OOM at some context length, a dataset namespacing change, an API key path that broke — should land in a single shared `constraints.md` file as one terse `C-NNN` bullet. The Architect reads this file at design time; the next spec won't re-issue something the box can't honor. **This is the single biggest reason the loop gets faster over time.** Treat it as the shared memory across runs.

### Veto window before code-gen

`cbq cancel <id>` works for tasks still in `enqueued` (before the architect drafts code). Use it liberally — `auto-suggest` may propose 5 experiments when only 2 are worth running. Killing 3 in the veto window costs nothing.

### `cbq stop <id>` for in-flight

For tasks already `executing`, `cbq stop <id>` sets a DB flag that the worker watcher reconciles within ~60s, SIGTERMs the process, and heals the slot. Use this when you spot a wedged run.

### Watch the leases, not the heartbeats

The queue tracks worker liveness via `cbq heartbeat`. If a worker box dies mid-run, `cbq reap` returns the stale claim to the queue so another worker (or a respin after fixing the box) can pick it up. Schedule `cbq reap` as a 5-minute cron.

### Touch the claim, not the row

Each per-attempt step inside a phase should call `cbq touch <id>` to refresh `claimed_at` — this prevents the reaper from yanking a slow-but-live multi-attempt session out from under you.

## What this is *not*

- Not a training framework (bring your own `transformers` / `trl` / etc.)
- Not a workflow orchestrator like Airflow or Prefect (no DAGs; the DAG is the agent loop)
- Not Ray (no distributed compute primitives; each task runs on one box)
- Not Modal / Beam (no managed infra; you bring the boxes)

Closest analogues are home-grown lab queues, `slurm` job arrays, or hand-rolled Postgres queues. This one is opinionated about the **agent loop** layered on top.

## License

Apache 2.0. See `LICENSE`.
