# runner/ — orchestration scripts around `cbq`

Thin shell scripts that wrap a Claude Code session for each phase of the loop. The agents *are* these scripts — there is no separate daemon. Each watcher polls the queue, claims a row, spawns the right Claude Code session with the right prompt, and writes the right `cbq` transition when the session exits.

## Scripts

| File | Purpose |
|---|---|
| `architect_up.sh` | Start the architect watcher loop on a CPU box (one-shot bootstrap). |
| `architect_watcher.sh` | Inner loop: claims `enqueued`/`executed` rows and dispatches to code or analyze. |
| `architect_code_task.sh` | CODE phase: spawn Claude Code, draft `run.spec` + code on `exp/NNNN-*` branch, push, mark `ready`. |
| `architect_analyze_task.sh` | ANALYZE phase: spawn Claude Code, read execution report, file verdict (`done` / `falsified` / `blocked`). |
| `suggest_experiment.sh` | After a verdict: spawn Claude Code with the parent context + constraints, propose the next experiment, `cbq submit` it. |
| `worker_up.sh` | Start the worker watcher loop on a GPU box. |
| `worker_watcher.sh` | Inner loop: claims `ready` rows; runs the experiment; emits results. |
| `worker_shift.sh` | The per-experiment runner — checkout branch, run smoke gate, run training, write `results.json`, `cbq executed`. |
| `worker_reconcile.sh` | Reconciles `cbq stop <id>` requests — SIGTERMs in-flight training when a human kills the row. |
| `smoke_and_launch.sh` | Smoke-gate pattern: run a 1-step sanity check before the full job. |
| `finalize_completed.sh` | After verdict: upload trajectory artifacts to S3, render archive file, commit. |
| `loadtest_coder_lane.sh` | Variant of code-task lane for `kind=loadtest` experiments (independent slot). |
| `publish_slots.sh` | Publishes free-slot counts to CloudWatch (drives the worker-allocation dashboard). |
| `migrate_watcher_to_systemd.sh` | One-shot helper to install the systemd units from a tmux/cron setup. |
| `architect.service` / `architect-loadtest.service` / `worker.service` | Systemd unit files for the watchers. |

## Required env vars

| Variable | Used by | Default |
|---|---|---|
| `CB_QUEUE_DB_URL` | All `cbq` calls | (required) |
| `CBQ_ACTOR` | Audit trail; e.g. `worker:my-h100` | `<role>:<hostname>` |
| `CBQ` | Path to the `cbq` binary | `$HOME/bin/cbq` |
| `EXP_S3_BUCKET` | Upload destination for trajectory + architect logs | (required for S3 uploads) |
| `AWS_ACCOUNT_ID` | Embedded in some IAM role ARNs | (required for AWS publishers) |
| `MODEL_S3_BUCKET` | Where pretrained weights live | (optional) |
| `EXPERIMENTS_DIR` | Where archive files (`done/`, `falsified/`, `constraints.md`) live in your repo | (required for analyze phase) |
| `REPO_ROOT` | Your monorepo root (where `exp/NNNN-*` branches check out) | (required) |
| `SLACK_WEBHOOK_URL` | Optional: posts smoke/verdict pings | unset = silent |
| `CLAUDE_CODE_OAUTH_TOKEN` | Auth for the spawned Claude Code sessions | (required) |

The architect scripts re-read `~/.oat` on every launch — the pipeline monitor agent (or you) refreshes it when the subscription token cycles.

## Adapting to your project

The phase scripts are intentionally short. The actual *prompt* given to Claude Code is inline in each `architect_*.sh` and `suggest_experiment.sh`. To wire this to your project:

1. **`architect_code_task.sh`** — edit the prompt to reference your project's `lib/` package, code-style conventions, and any shared helpers. The smoke-gate pattern at the bottom (`smoke_and_launch.sh`) is the protocol every spawned `run.py` must follow.
2. **`architect_analyze_task.sh`** — edit the verdict rules to match what "done" vs "falsified" vs "blocked" means in your domain. The default rule is: `done` if the success criterion in the run.spec is met; `falsified` if the run completed but the criterion failed; `blocked` if a constraint prevents further progress (paired with a new `C-NNN` bullet in `${EXPERIMENTS_DIR}/constraints.md`).
3. **`suggest_experiment.sh`** — this is where the hypothesis tree lives. Replace the inline prompt with your domain's pivoting heuristics (e.g. *"on a +Δ verdict, propose a variant that tightens the lever; on a − verdict, propose an orthogonal mechanism"*).

The shell scripts themselves should rarely need editing — they're queue plumbing, attempt budgeting, log uploading, and Slack pings. Most customization happens in the prompts.

## Running on systemd

```bash
# install (one-time, after editing User= in the unit files):
sudo cp runner/architect.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now architect.service

# tail logs:
journalctl -u architect.service -f
```

The unit files assume `cbq` is on `PATH` and `CB_QUEUE_DB_URL` is set in the systemd environment (use `EnvironmentFile=` or `DROP_IN`).

## Running on a CPU dev box (no systemd)

```bash
tmux new -s architect
runner/architect_up.sh        # foreground loop; Ctrl-C to stop
```

## Notes on slot accounting

`publish_slots.sh` reads a per-host `slots.json` and writes `Free / Busy / Total` slot counts to CloudWatch. Optional — only useful if you have multiple GPU boxes and want a single-pane dashboard. The framework itself doesn't depend on it.
