# auto-research

An autonomous research loop: four LLM agents — **Architect**, **GPU Worker**, **Analyzer**, **Auto-suggester** — coordinate through a single Postgres queue to run hundreds of experiments while you sleep.

Originally built for ML research (LoRA fine-tuning sweeps, kernel ablations, layer-localization probes), but the pattern is general — anything that fits the shape *"draft a spec → run it → score it → propose the next spec"* can be queued through it.

![architecture](docs/architecture.svg)

## What's in the loop

| Agent | Role | Where it runs |
|---|---|---|
| **Architect** | Turns a research question into a concrete `run.spec` + code on a fresh branch. | CPU-only orchestrator box |
| **GPU Worker** | Claims `ready` tasks, runs training + eval, writes results back. Calls a debug LLM when something non-routine breaks. | GPU box (H100 / A10 / whatever) |
| **Analyzer** | Reads the execution report, files a verdict (`done` / `falsified` / `blocked`). | CPU-only orchestrator box |
| **Auto-suggester** | Proposes the next experiment based on the verdict. Enqueues it; the Architect drafts code for it; the cycle repeats. | CPU-only orchestrator box |

Humans are the **overseer**, not the operator: review the queue, cancel runs that are wasting compute, file new constraints when something subtle breaks. The rest is the loop.

## State machine

```
enqueued → coding → ready → executing → executed → analyzing
analyzing → done | falsified | blocked   ← scientific verdicts
coding    → code_failed                  ← parked: in-phase retries exhausted
analyzing → executed | analyze_stuck     ← one auto-retry, then parked
enqueued  → cancelled                    ← human veto window
```

The `experiments` table is **authoritative for live state**. Git holds only the archive exports (`done/`, `falsified/`) and your per-experiment `exp/NNNN-*` branches.

## Quick start

### 1. Stand up a Postgres database

Any Postgres ≥ 13 works (local, RDS, Neon, Supabase, whatever).

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

See `examples/toy_sweep/` for a minimal end-to-end loop with a stub worker (no GPU required).

```bash
cd examples/toy_sweep
cbq submit spec.yaml --slug hello-world --kind research
cbq list --status enqueued
```

### 4. Run the loop

You can run the agents however you like — systemd units in `runner/`, tmux watchers, cron, Kubernetes. The pattern is:

```bash
# Architect side (CPU box)
runner/architect_watcher.sh           # claims enqueued → coding → ready

# Worker side (GPU box)
runner/worker_watcher.sh              # claims ready → executing → executed

# Architect side again (CPU box)
runner/architect_watcher.sh           # claims executed → analyzing → done/falsified/blocked
                                       # then runs suggest_experiment.sh → enqueues next
```

## Defining your own experiments

Every experiment is two things: a **`run.spec`** (YAML) and **`run.py`** (or whatever your worker invokes). The Architect writes both. Your worker code receives the spec path, runs whatever it runs, and writes a `results.json`. The Analyzer reads `results.json` + your scoring logic and files the verdict.

The framework doesn't care what your worker actually does — train a model, sweep a hyperparameter, run a backtest, generate synthetic data. It cares about the **lifecycle**: claim, execute, report, verdict, propose next.

## Operational tips

### The constraints pattern

Every novel failure — a kernel bug, an OOM at some context length, a dataset namespacing change, an API key path that broke — should land in a single shared `constraints.md` file as one terse `C-NNN` bullet. The Architect reads this file at design time; the next spec won't re-issue something the box can't honor. **This is the single biggest reason the loop gets faster over time.** Treat it as the team's shared memory.

### Veto window before code-gen

`cbq cancel <id>` works for tasks still in `enqueued` (before the architect drafts code). Use it liberally — `auto-suggest` may propose 5 experiments when only 2 are worth running. Killing 3 in the veto window costs nothing.

### `cbq stop <id>` for in-flight

For tasks already `executing`, `cbq stop <id>` sets a DB flag that the worker watcher reconciles within ~60s, SIGTERMs the process, and heals the slot. Use this when you spot a wedged training run.

### Watch the leases, not the heartbeats

The queue tracks worker liveness via `cbq heartbeat`. If a worker box dies mid-run, `cbq reap` returns the stale claim to the queue so another worker (or a respin after fixing the box) can pick it up. Schedule `cbq reap` as a 5-minute cron.

### Touch the claim, not the row

Each per-attempt step inside a phase should call `cbq touch <id>` to refresh `claimed_at` — this prevents the reaper from yanking a slow-but-live multi-attempt session out from under you.

## Layout

```
auto-research/
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
├── examples/toy_sweep/    # minimal end-to-end loop (no GPU needed)
└── docs/                  # architecture diagram
```

## What this is *not*

- Not a training framework (bring your own `transformers` / `trl` / etc.)
- Not a workflow orchestrator like Airflow or Prefect (no DAGs; the DAG is the agent loop)
- Not Ray (no distributed compute primitives; each task runs on one box)
- Not Modal / Beam (no managed infra; you bring the boxes)

Closest analogues are home-grown lab queues, `slurm` job arrays, or hand-rolled Postgres queues. This one is opinionated about the **agent loop** layered on top.

## License

Apache 2.0. See `LICENSE`.

## Acknowledgments

Built and battle-tested on a 108-experiment LoRA fine-tuning sweep that ran for 3 days mostly autonomously. Read the write-up at [coralbricks.ai/research/lora-trajectory](https://coralbricks.ai/research/lora-trajectory).
