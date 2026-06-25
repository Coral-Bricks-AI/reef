# toy_sweep — minimal end-to-end loop

A 5-minute walkthrough of the agent loop with no GPU and no LLM cost. The "worker" is a Python stub that sleeps and writes a deterministic accuracy curve so the auto-suggester has something real to climb.

## Run it

```bash
# 1. point cbq at a Postgres database
export CB_QUEUE_DB_URL=postgres://localhost/cbq_dev
cbq init-db

# 2. submit the task
cbq submit examples/toy_sweep/spec.yaml --slug toy-lr-sweep --kind research

# 3. simulate the architect drafting code
ID=$(cbq list --status enqueued --json | python -c "import json,sys; print(json.load(sys.stdin)[0]['id'])")
cbq claim coding --kind research --machine cpu --actor architect-demo
cbq ready "$ID"      # mark code complete; experiment moves to ready

# 4. simulate the worker running it
cbq claim executing --kind research --machine cpu --actor worker-demo
mkdir -p /tmp/cbq-results/$ID
python examples/toy_sweep/run.py examples/toy_sweep/spec.yaml /tmp/cbq-results/$ID
cbq executed "$ID" --report /tmp/cbq-results/$ID/results.json

# 5. simulate the analyzer
cbq claim analyzing --kind research --machine cpu --actor analyzer-demo
cbq verdict "$ID" done --notes "stub run completed"

cbq show "$ID"       # see the full row
```

## What's not shown

In a real deployment the `architect_*.sh` / `worker_*.sh` watchers in `runner/` do all of steps 3–5 automatically — they claim, spawn a Claude Code session with the appropriate prompt, and write the right `cbq` transitions when the session finishes. This example fakes those transitions by hand so you can see the state machine working.

To wire up real LLM-driven agents:

1. Adapt `runner/architect_code_task.sh` to point at your project's `lib/` helpers and code-style conventions.
2. Adapt `runner/architect_analyze_task.sh` to point at your scoring + verdict rules.
3. Adapt `runner/suggest_experiment.sh` with a prompt that knows your hypothesis tree.
4. Start `runner/architect_watcher.sh` and `runner/worker_watcher.sh` (or use the systemd units in `runner/*.service`).
