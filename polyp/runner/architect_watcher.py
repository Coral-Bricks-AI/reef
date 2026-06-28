"""``polyp.runner.architect_watcher`` -- the architect-side poll/dispatch loop.

Ported from ``architect_watcher.sh``. Runs on the CPU orchestrator,
polls cb_queue via cbq, and dispatches phase sessions:

- ``executed`` rows -> :mod:`polyp.runner.architect_analyze_task`
  (iterator; one per row, up to ``ANALYZE_MAX``)
- ``enqueued`` rows -> :mod:`polyp.runner.architect_code_task`
  (coder; up to ``CODE_SLOTS``)
- per (kind, machine) with a live worker heartbeat where iteration
  is quiet and the live count is below GPU+1:
  :mod:`polyp.runner.suggest_experiment` (generator)

Claims are atomic DB row updates (`cbq claim`, `FOR UPDATE SKIP LOCKED`);
this loop only decides WHO to claim and WHEN to fire. Phase sessions
that crash leave their row claimed; ``cbq reap`` returns stale claims
each poll.

Invoke as ``python -m polyp.runner.architect_watcher``.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from polyp.runner.common import cbq, cbq_bin, iso_now, log_line


WORKPOOL = Path(os.environ.get(
    "WORKPOOL", str(Path.home() / "architect")
)).expanduser()
SUGGEST_LOG = WORKPOOL / "auto-suggest.log"
LOGS_DIR = WORKPOOL / "logs"
POLL_SEC = int(os.environ.get("POLL_SEC", "30"))
SUGGEST_INTERVAL_SEC = int(os.environ.get("SUGGEST_INTERVAL_SEC", "0"))
WORKER_ACTIVE_MIN = int(os.environ.get("WORKER_ACTIVE_MIN", "5"))
SUGGEST_EXCLUDE_KINDS = set(
    os.environ.get(
        "SUGGEST_EXCLUDE_KINDS", "loadtest swe-bench swe-bench-sweep"
    ).split()
)
CODE_SLOTS = int(os.environ.get("CODE_SLOTS", "2"))
ANALYZE_MAX = int(os.environ.get("ANALYZE_MAX", "8"))


# Python module entrypoints replacing the bash scripts. We launch a
# fresh ``python -m polyp.runner.<mod>`` so each phase gets its own
# process (matches the original ``& ; pid=$!`` lifecycle).
ANALYZE_MODULE = "polyp.runner.architect_analyze_task"
CODE_MODULE = "polyp.runner.architect_code_task"
SUGGEST_MODULE = "polyp.runner.suggest_experiment"


def ensure_clone(directory: Path) -> bool:
    """Clone the www repo into ``directory`` if absent; widen its refspec.

    Returns False on clone failure (caller should unclaim the row).
    """
    if (directory / ".git").exists():
        # Widen refspec idempotently so exp/* branches can be fetched.
        subprocess.run(
            ["git", "-C", str(directory), "config", "remote.origin.fetch",
             "+refs/heads/*:refs/remotes/origin/*"],
            text=True, capture_output=True,
        )
        return True
    log_line(f"cloning phase workdir {directory}...")
    directory.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["gh", "repo", "clone", "Coral-Bricks-AI/www", str(directory),
         "--", "--depth", "50"],
        text=True, capture_output=True,
    )
    if result.returncode != 0:
        log_line(f"clone failed: {result.stderr}")
        return False
    subprocess.run(
        ["git", "-C", str(directory), "config", "remote.origin.fetch",
         "+refs/heads/*:refs/remotes/origin/*"],
        text=True, capture_output=True,
    )
    return True


def free_idx(pids: dict[int, subprocess.Popen], max_n: int) -> Optional[int]:
    """Return the lowest 1..max_n index with no live process, else None."""
    for i in range(1, max_n + 1):
        proc = pids.get(i)
        if proc is None or proc.poll() is not None:
            return i
    return None


def cbq_counts_json(*extra: str) -> Optional[dict]:
    """``cbq counts [...] --json`` parsed; None on failure."""
    res = cbq("counts", *extra, "--json", quiet=True)
    if res.returncode != 0:
        return None
    try:
        return json.loads(res.stdout or "{}")
    except json.JSONDecodeError:
        return None


def cbq_workers_groups(active_min: int) -> list[tuple[str, str, int]]:
    """Return (kind, machine, gpu_count) per active (kind, machine) group.

    Replaces the bash ``cbq workers --json | jq group_by(...)`` pipeline.
    A heartbeat without ``gpus`` counts as 1 GPU so its kind still gets
    work; an empty kind becomes ``"any"``.
    """
    res = cbq("workers", "--active-min", str(active_min), "--json", quiet=True)
    if res.returncode != 0:
        return []
    try:
        rows = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        return []
    grouped: dict[tuple[str, str], int] = {}
    for row in rows:
        kind = (row.get("kind") or "any") or "any"
        machine = row.get("machine") or ""
        gpus_csv = (row.get("gpus") or "")
        gpu_count = len([g for g in gpus_csv.split(",") if g])
        if gpu_count == 0:
            gpu_count = 1
        key = (kind, machine)
        grouped[key] = grouped.get(key, 0) + gpu_count
    return [(k[0], k[1], n) for k, n in grouped.items()]


def spawn_phase(
    *, module: str, id_: str, workdir: Path,
    extra_env: Optional[dict[str, str]] = None,
) -> subprocess.Popen:
    """Fork off ``python -m <module> <id>`` with ``WORKDIR`` set."""
    env = dict(os.environ)
    env["WORKDIR"] = str(workdir)
    if extra_env:
        env.update(extra_env)
    argv = [sys.executable, "-m", module, id_]
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    # Inherit stdout/stderr -- the phase script writes its own structured
    # log file via the common helpers; the parent stream just shows the
    # top-line progress lines so a `journalctl -u architect` is useful.
    return subprocess.Popen(argv, env=env)


def spawn_suggest(
    *, kind: str, machine: str, workdir: Path,
) -> subprocess.Popen:
    env = dict(os.environ)
    env["WORKDIR"] = str(workdir)
    env["SUGGEST_KIND"] = "" if kind == "any" else kind
    env["SUGGEST_MACHINE"] = machine
    argv = [sys.executable, "-m", SUGGEST_MODULE]
    return subprocess.Popen(argv, env=env)


def run_loop() -> int:
    os.environ.setdefault("CBQ_ACTOR", f"architect:{socket.gethostname()}")
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    analyze_pids: dict[int, subprocess.Popen] = {}
    code_pids: dict[int, subprocess.Popen] = {}
    suggest_pids: dict[tuple[str, str], subprocess.Popen] = {}
    excluded_logged: set[str] = set()

    log_line(
        f"architect watcher up (pid={os.getpid()}, poll={POLL_SEC}s, "
        f"low-water=per-group gpus+1, worker-active={WORKER_ACTIVE_MIN}m)"
    )

    while True:
        # Crashed phase sessions leave rows claimed.
        cbq("reap", "--quiet", quiet=True)

        counts = cbq_counts_json()
        if counts is None:
            log_line(
                f"cbq counts failed (db unreachable?); retrying in {POLL_SEC}s"
            )
            time.sleep(POLL_SEC)
            continue
        n_enqueued = int(counts.get("enqueued", 0) or 0)
        n_executed = int(counts.get("executed", 0) or 0)

        # ---- iterators (one per completed GPU job) ----
        while n_executed > 0:
            idx = free_idx(analyze_pids, ANALYZE_MAX)
            if idx is None:
                log_line(
                    f"ANALYZE_MAX={ANALYZE_MAX} iterators busy; remaining "
                    f"executed rows wait for the next poll"
                )
                break
            claim = cbq("claim", "analyze",
                        "--kind-not-in", "swe-bench,swe-bench-sweep",
                        quiet=True)
            if claim.returncode != 0:
                break
            id_ = (claim.stdout or "").strip()
            if not id_:
                break
            directory = WORKPOOL / f"work-analyze-{idx}" / "www"
            if not ensure_clone(directory):
                cbq("unclaim", id_, "--note", "phase clone failed", quiet=True)
                break
            log_line(f"iterator[{idx}] spawned for: {id_}")
            analyze_pids[idx] = spawn_phase(
                module=ANALYZE_MODULE, id_=id_, workdir=directory,
            )
            n_executed -= 1

        # ---- code pool (turn enqueued tasks into ready specs) ----
        while n_enqueued > 0:
            idx = free_idx(code_pids, CODE_SLOTS)
            if idx is None:
                break
            claim = cbq("claim", "code",
                        "--kind-not-in", "swe-bench,swe-bench-sweep",
                        quiet=True)
            if claim.returncode != 0:
                break
            id_ = (claim.stdout or "").strip()
            if not id_:
                break
            directory = WORKPOOL / f"work-code-{idx}" / "www"
            if not ensure_clone(directory):
                cbq("unclaim", id_, "--note", "phase clone failed", quiet=True)
                break
            log_line(f"coder[{idx}] spawned for: {id_}")
            code_pids[idx] = spawn_phase(
                module=CODE_MODULE, id_=id_, workdir=directory,
            )
            n_enqueued -= 1

        # ---- generator: per (kind, machine) group with a live worker ----
        for kind, machine, gpu_count in cbq_workers_groups(WORKER_ACTIVE_MIN):
            if kind in SUGGEST_EXCLUDE_KINDS:
                if kind not in excluded_logged:
                    log_line(
                        f"kind {kind} excluded from auto-suggest "
                        f"(SUGGEST_EXCLUDE_KINDS; logged once per watcher "
                        f"start)"
                    )
                    excluded_logged.add(kind)
                continue
            low_water = gpu_count + 1
            kind_args = [] if kind == "any" else ["--kind", kind]
            group_counts = cbq_counts_json(*kind_args, "--machine", machine)
            if group_counts is None:
                continue
            live = int(
                (group_counts.get("enqueued", 0) or 0)
                + (group_counts.get("coding", 0) or 0)
                + (group_counts.get("ready", 0) or 0)
                + (group_counts.get("executing", 0) or 0)
            )
            iterating = int(
                (group_counts.get("executed", 0) or 0)
                + (group_counts.get("analyzing", 0) or 0)
            )
            if iterating > 0 or live >= low_water:
                continue
            key = (kind, machine)
            prev = suggest_pids.get(key)
            if prev is not None and prev.poll() is None:
                continue
            now = int(time.time())
            stamp_path = WORKPOOL / f".last_suggest.{kind}.{machine}"
            try:
                last = int(stamp_path.read_text().strip())
            except (OSError, ValueError):
                last = 0
            if now - last < SUGGEST_INTERVAL_SEC:
                continue
            log_line(
                f"{kind}/{machine} quiet, live={live} < {low_water} "
                f"({gpu_count} gpus + 1); firing generator"
            )
            shared_workdir = WORKPOOL / "workdir" / "www"
            if not ensure_clone(shared_workdir):
                continue
            stamp_path.write_text(str(now))
            suggest_pids[key] = spawn_suggest(
                kind=kind, machine=machine, workdir=shared_workdir,
            )

        time.sleep(POLL_SEC)


def main(argv: Optional[list[str]] = None) -> int:
    # Graceful shutdown: forward SIGTERM to children so systemd stop
    # doesn't leave orphan phase sessions behind.
    def _bye(signum, _frame):
        log_line(f"architect watcher caught signal {signum}; exiting")
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)
    return run_loop()


if __name__ == "__main__":
    sys.exit(main() or 0)
