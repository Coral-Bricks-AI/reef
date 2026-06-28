"""``polyp.runner.worker_watcher`` -- the GPU worker's watchdog loop.

Ported from ``worker_watcher.sh``. Runs on the GPU box. Does NO
scheduling itself — the worker Claude session (``worker_shift``) owns
placement, fixing, and finalization. This loop only detects EVENTS and
fires a shift, so no Claude tokens burn while jobs are happily running:

- ``ready`` rows in cb_queue AND at least one slot is free
- a registered job's pid is dead (needs finalize or fix)
- a running job's progress.log has stalled (> ``STALL_SEC`` without writes)
- an ``executing`` row claimed by this host holds no slot entry (orphan)
- heartbeat: jobs are running and the last shift was > ``HEARTBEAT_SEC`` ago

State written by the worker shift: ``~/worker/slots.json``. Per-retry
bookkeeping owned here: ``~/worker/.exec_seen.json``,
``~/worker/.dead_suppress.json``.

Invoke as ``python -m polyp.runner.worker_watcher``.
"""

from __future__ import annotations

import json
import os
import random
import re
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from polyp.runner.common import cbq, iso_now, log_line


BASE = Path(os.environ.get("WORKER_BASE", str(Path.home() / "worker" / "www")))
WORKER_HOME = BASE.parent
POLL_SEC = int(os.environ.get("POLL_SEC", "60"))
HEARTBEAT_SEC = int(os.environ.get("HEARTBEAT_SEC", "600"))
STALL_SEC = int(os.environ.get("STALL_SEC", "900"))
WORKER_GPUS = os.environ.get("WORKER_GPUS", "0")
SLOTS_PATH = WORKER_HOME / "slots.json"
LAST_SHIFT_FILE = WORKER_HOME / ".last_shift"
SEEN_PATH = WORKER_HOME / ".exec_seen.json"
SUPPRESS_PATH = WORKER_HOME / ".dead_suppress.json"
CRASH_LOOP_DEATHS = int(os.environ.get("CRASH_LOOP_DEATHS", "3"))
SUPPRESS_SEC = int(os.environ.get("SUPPRESS_SEC", "1800"))
MAX_BACKOFF_SEC = int(os.environ.get("MAX_BACKOFF_SEC", "1800"))
HEARTBEAT_EVERY_SEC = int(os.environ.get("HEARTBEAT_EVERY_SEC", "60"))
FAIL_WINDOW_MIN = int(os.environ.get("FAIL_WINDOW_MIN", "5"))
INFLIGHT_DIR = WORKER_HOME / "inflight_shifts"

CBQ_KIND = (os.environ.get("CBQ_KIND") or "").strip()
CBQ_MACHINE = (os.environ.get("CBQ_MACHINE") or "a10").strip()
CB_LEASE_RESOURCE = (os.environ.get("CB_LEASE_RESOURCE") or "").strip()
WORKER_ACTOR = (os.environ.get("CBQ_ACTOR") or f"worker:{socket.gethostname()}")


def _kind_args() -> list[str]:
    return ["--kind", CBQ_KIND] if CBQ_KIND else []


def _schedulable_gpu_count() -> int:
    n = len([g for g in WORKER_GPUS.split(",") if g])
    return max(1, min(4, n))


MAX_INFLIGHT_SHIFTS = int(os.environ.get(
    "MAX_INFLIGHT_SHIFTS", str(_schedulable_gpu_count())
))

WORKER_SHIFT_MODULE = "polyp.runner.worker_shift"
WORKER_RECONCILE_MODULE = "polyp.runner.worker_reconcile"


# ----------------------------------------------------------------------------
# JSON state file helpers (atomic via tempfile + rename)
# ----------------------------------------------------------------------------


def _read_json(path: Path, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _write_json_atomic(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f)
    tmp.replace(path)


def jqset(path: Path, key: str, value) -> None:
    """Atomic single-key update on a small JSON dict file."""
    data = _read_json(path, {})
    if not isinstance(data, dict):
        data = {}
    if value is None:
        data.pop(key, None)
    else:
        data[key] = value
    _write_json_atomic(path, data)


# ----------------------------------------------------------------------------
# Self-heal: git credentials + global config helper
# ----------------------------------------------------------------------------


def self_heal_git_credentials() -> None:
    """Restore ~/.git-credentials from secrets manager if zeroed.

    A prior shift session has been observed truncating the file mid-run;
    the watcher restores it deterministically so the next fetch doesn't
    hang on a username prompt.
    """
    creds = Path.home() / ".git-credentials"
    if creds.exists() and creds.stat().st_size > 0:
        return
    res = subprocess.run(
        ["aws", "secretsmanager", "get-secret-value",
         "--secret-id", "prod/github/deploy-token",
         "--query", "SecretString", "--output", "text"],
        text=True, capture_output=True,
        env={**os.environ, "AWS_DEFAULT_REGION": "us-east-1"},
    )
    if res.returncode != 0:
        log_line(
            "self-heal SKIPPED: cannot read prod/github/deploy-token (IAM?); "
            "next git fetch will hang"
        )
        return
    try:
        payload = json.loads(res.stdout or "{}")
    except json.JSONDecodeError:
        return
    token = payload.get("GITHUB_TOKEN")
    if not token or token == "null":
        return
    creds.write_text(f"https://x-access-token:{token}@github.com\n")
    os.chmod(creds, 0o600)
    log_line("self-heal: restored ~/.git-credentials from secrets manager")


def self_heal_github_credential_helper() -> None:
    """Strip the host-specific credential helper ``gh auth setup-git`` installs.

    On Ubuntu 22.04 the broken ``gh auth git-credential`` route makes
    every ``git fetch`` hang on a username prompt even when
    ``~/.git-credentials`` is intact. Idempotent strip.
    """
    res = subprocess.run(
        ["git", "config", "--global", "--get",
         "credential.https://github.com.helper"],
        text=True, capture_output=True,
    )
    if res.returncode != 0:
        return
    subprocess.run(
        ["git", "config", "--global", "--unset-all",
         "credential.https://github.com.helper"],
        text=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "--global", "--remove-section",
         "credential.https://github.com"],
        text=True, capture_output=True,
    )
    log_line(
        "self-heal: stripped broken credential.https://github.com.helper "
        "(gh auth setup-git override)"
    )


def ensure_base_clone() -> None:
    if (BASE / ".git").exists():
        return
    log_line(f"cloning Coral-Bricks-AI/www to {BASE}...")
    BASE.parent.mkdir(parents=True, exist_ok=True)
    res = subprocess.run(
        ["gh", "repo", "clone", "Coral-Bricks-AI/www", str(BASE), "--",
         "--depth", "50"],
        text=True, capture_output=True,
    )
    if res.returncode != 0:
        raise SystemExit(f"clone failed: {res.stderr}")
    subprocess.run(
        ["git", "-C", str(BASE), "config", "remote.origin.fetch",
         "+refs/heads/*:refs/remotes/origin/*"],
        text=True, capture_output=True,
    )


def reset_base_to_main() -> None:
    lock = BASE / ".git" / "index.lock"
    if lock.exists():
        lock.unlink()
    subprocess.run(["git", "checkout", "main", "--quiet"],
                   cwd=str(BASE), text=True, capture_output=True)
    if lock.exists():
        lock.unlink()
    subprocess.run(["git", "fetch", "origin", "main", "--quiet"],
                   cwd=str(BASE), text=True, capture_output=True)
    subprocess.run(["git", "reset", "--hard", "origin/main", "--quiet"],
                   cwd=str(BASE), text=True, capture_output=True)


# ----------------------------------------------------------------------------
# Slot / job iteration
# ----------------------------------------------------------------------------


def init_slot_table() -> None:
    if SLOTS_PATH.exists():
        return
    table = {gpu: None for gpu in WORKER_GPUS.split(",") if gpu}
    _write_json_atomic(SLOTS_PATH, table)
    log_line(f"initialized slot table for GPUs {WORKER_GPUS}")


def free_slot_count() -> int:
    slots = _read_json(SLOTS_PATH, {})
    if not isinstance(slots, dict):
        return 0
    return sum(1 for v in slots.values() if v is None)


def distinct_running_jobs() -> list[tuple[str, dict]]:
    """Return ``[(job_id, first_slot_entry), ...]`` for every job currently
    occupying at least one slot."""
    slots = _read_json(SLOTS_PATH, {})
    if not isinstance(slots, dict):
        return []
    seen: dict[str, dict] = {}
    for entry in slots.values():
        if entry is None or not isinstance(entry, dict):
            continue
        job = entry.get("job")
        if not job:
            continue
        seen.setdefault(job, entry)
    return list(seen.items())


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def progress_log_path(slot_dir: str, job: str) -> Optional[Path]:
    candidate = (
        Path(slot_dir) / "ml" / "eval" / "experiments" / "results" / job
        / "progress.log"
    )
    if candidate.exists():
        return candidate
    fallback = Path(slot_dir) / "progress.log"
    if fallback.exists():
        return fallback
    return None


def file_stalled(path: Path, threshold_s: int) -> bool:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    return (time.time() - mtime) > threshold_s


def parse_started(started_str: str) -> int:
    if not started_str:
        return int(time.time())
    # Accept ISO-8601 with optional Z; fall back to date(1) parse.
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return int(datetime.strptime(started_str, fmt).timestamp())
        except ValueError:
            continue
    res = subprocess.run(["date", "-d", started_str, "+%s"],
                         text=True, capture_output=True)
    if res.returncode == 0:
        try:
            return int(res.stdout.strip())
        except ValueError:
            pass
    return int(time.time())


# ----------------------------------------------------------------------------
# Inflight shift tracking
# ----------------------------------------------------------------------------


def count_inflight() -> int:
    """Count live shift PIDs; GC pidfiles whose process has exited."""
    INFLIGHT_DIR.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in INFLIGHT_DIR.glob("*.pid"):
        try:
            pid = int(f.read_text().strip())
        except (OSError, ValueError):
            f.unlink(missing_ok=True)
            continue
        if pid_alive(pid):
            n += 1
        else:
            f.unlink(missing_ok=True)
    return n


def count_recent_failures(window_min: int) -> int:
    if not INFLIGHT_DIR.exists():
        return 0
    cutoff = time.time() - window_min * 60
    return sum(
        1 for f in INFLIGHT_DIR.glob("*.failed")
        if f.stat().st_mtime >= cutoff
    )


def gc_old_failures() -> None:
    cutoff = time.time() - 60 * 60
    for f in INFLIGHT_DIR.glob("*.failed"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def spawn_shift(reasons: str) -> int:
    """Background a ``worker_shift`` process; return its PID.

    Writes a ``.pid`` marker into INFLIGHT_DIR which the count loop and
    failure-tracking inspect.
    """
    INFLIGHT_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{int(time.time())}-{os.getpid()}-{random.randint(0, 99999)}"
    pidfile = INFLIGHT_DIR / f"{stem}.pid"
    failmarker = INFLIGHT_DIR / f"{stem}.failed"
    # The child writes its own failure marker on non-zero exit; the parent
    # only records the pid so the inflight counter sees it.
    argv = [sys.executable, "-m", WORKER_SHIFT_MODULE, reasons]
    env = dict(os.environ)
    env["WORKER_SHIFT_PIDFILE"] = str(pidfile)
    env["WORKER_SHIFT_FAILMARKER"] = str(failmarker)
    proc = subprocess.Popen(argv, env=env)
    pidfile.write_text(str(proc.pid))
    return proc.pid


def run_reconcile() -> None:
    """Phase A/B reconcile -- best-effort, never fatal."""
    log_path = WORKER_HOME / "reconcile.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", buffering=1) as f:
        res = subprocess.run(
            [sys.executable, "-m", WORKER_RECONCILE_MODULE],
            text=True, stdout=f, stderr=subprocess.STDOUT,
            env=os.environ,
        )
    if res.returncode != 0:
        log_line("worker_reconcile exited nonzero (non-fatal)")


# ----------------------------------------------------------------------------
# Background heartbeat
# ----------------------------------------------------------------------------


def heartbeat_forever() -> None:
    """Background heartbeat so the architect sees this kind as live even
    while the main loop blocks on a shift."""
    while True:
        try:
            cbq("heartbeat", *_kind_args(), "--machine", CBQ_MACHINE,
                "--gpus", WORKER_GPUS, quiet=True)
        except (FileNotFoundError, subprocess.SubprocessError):
            pass
        time.sleep(HEARTBEAT_EVERY_SEC)


def spawn_heartbeat() -> subprocess.Popen:
    """Fork a child whose only job is the heartbeat loop."""
    argv = [
        sys.executable, "-c",
        "from polyp.runner.worker_watcher import heartbeat_forever; "
        "heartbeat_forever()",
    ]
    return subprocess.Popen(argv, env=os.environ)


# ----------------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------------


def run_loop() -> int:
    WORKER_HOME.mkdir(parents=True, exist_ok=True)
    (WORKER_HOME / "logs").mkdir(exist_ok=True)
    (WORKER_HOME / "inflight").mkdir(exist_ok=True)
    (WORKER_HOME / "jobs").mkdir(exist_ok=True)
    INFLIGHT_DIR.mkdir(parents=True, exist_ok=True)
    for p in (SEEN_PATH, SUPPRESS_PATH):
        if not p.exists() or p.stat().st_size == 0:
            _write_json_atomic(p, {})

    os.environ["CBQ_ACTOR"] = WORKER_ACTOR
    os.environ["CBQ_MACHINE"] = CBQ_MACHINE
    os.environ["WORKER_GPUS"] = WORKER_GPUS

    (BASE / ".git" / "index.lock").unlink(missing_ok=True) \
        if (BASE / ".git" / "index.lock").exists() else None
    ensure_base_clone()
    init_slot_table()

    log_line(
        f"worker watchdog up (pid={os.getpid()}, gpus={WORKER_GPUS}, "
        f"poll={POLL_SEC}s, heartbeat={HEARTBEAT_SEC}s, stall={STALL_SEC}s)"
    )

    heartbeat_proc = spawn_heartbeat()

    def _bye(signum, _frame):
        log_line(f"worker watchdog caught signal {signum}; exiting")
        try:
            heartbeat_proc.terminate()
        except OSError:
            pass
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)

    while True:
        try:
            self_heal_git_credentials()
            self_heal_github_credential_helper()
            reset_base_to_main()

            counts_res = cbq("counts", *_kind_args(), "--json", quiet=True)
            if counts_res.returncode != 0:
                log_line(
                    f"cbq counts failed (db unreachable?); retrying in "
                    f"{POLL_SEC}s"
                )
                time.sleep(POLL_SEC)
                continue
            try:
                counts = json.loads(counts_res.stdout or "{}")
            except json.JSONDecodeError:
                counts = {}
            n_ready = int(counts.get("ready", 0) or 0)

            drain = ""
            if CB_LEASE_RESOURCE:
                lease_res = cbq("lease-active", CB_LEASE_RESOURCE, quiet=True)
                first_line = (lease_res.stdout or "").splitlines()[:1]
                if first_line and first_line[0].strip():
                    drain = first_line[0].strip()

            run_reconcile()
            # Phase B may have claimed ready specs; refresh.
            counts_res = cbq("counts", *_kind_args(), "--json", quiet=True)
            if counts_res.returncode == 0:
                try:
                    counts = json.loads(counts_res.stdout or "{}")
                    n_ready = int(counts.get("ready", 0) or 0)
                except json.JSONDecodeError:
                    pass

            free = free_slot_count()

            dead, stalled, running, park_reasons = scan_running_jobs()

            orphans = count_orphan_executing()

            now = int(time.time())
            last_shift = 0
            if LAST_SHIFT_FILE.exists():
                try:
                    last_shift = int(LAST_SHIFT_FILE.read_text().strip())
                except (OSError, ValueError):
                    last_shift = 0

            reasons_parts: list[str] = []
            if dead > 0:
                reasons_parts.append(f"{dead} job(s) finished or died")
            if stalled > 0:
                reasons_parts.append(
                    f"{stalled} job(s) stalled >{STALL_SEC // 60}min"
                )
            if orphans > 0:
                reasons_parts.append(
                    f"{orphans} orphaned executing claim(s) with no slot"
                )
            if not drain:
                if n_ready > 0 and free > 0:
                    reasons_parts.append(
                        f"{n_ready} ready spec(s) with {free} free GPU(s)"
                    )
            elif n_ready > 0 and free > 0:
                log_line(
                    f"draining: node leased by {drain}; holding {n_ready} "
                    f"ready spec(s) off {free} free GPU(s)"
                )
            if running > 0 and (now - last_shift) >= HEARTBEAT_SEC:
                reasons_parts.append(
                    f"heartbeat observation ({running} running)"
                )

            reasons = "; ".join(reasons_parts)
            if reasons:
                reasons = reasons + "; "
            reasons = park_reasons + reasons

            if reasons:
                inflight = count_inflight()
                if inflight >= MAX_INFLIGHT_SHIFTS:
                    log_line(
                        f"skip shift (inflight={inflight}/"
                        f"{MAX_INFLIGHT_SHIFTS}): {reasons}"
                    )
                else:
                    shift_pid = spawn_shift(reasons)
                    log_line(
                        f"fired shift pid={shift_pid} "
                        f"(inflight={inflight + 1}/{MAX_INFLIGHT_SHIFTS}): "
                        f"{reasons}"
                    )
                    LAST_SHIFT_FILE.write_text(str(now))

            fail_n = count_recent_failures(FAIL_WINDOW_MIN)
            sleep_for = POLL_SEC
            if fail_n > 0:
                multiplier = 1 << min(fail_n, 5)
                sleep_for = min(POLL_SEC * multiplier, MAX_BACKOFF_SEC)
                log_line(
                    f"backing off {sleep_for}s after {fail_n} shift "
                    f"failure(s) in last {FAIL_WINDOW_MIN}m"
                )
            gc_old_failures()
            time.sleep(sleep_for)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 -- watchdog must not die
            log_line(f"worker watcher loop error: {exc!r}; sleeping {POLL_SEC}s")
            time.sleep(POLL_SEC)


def scan_running_jobs() -> tuple[int, int, int, str]:
    """Walk every job in the slot table; return (dead, stalled, running,
    park_reasons)."""
    now = int(time.time())
    seen = _read_json(SEEN_PATH, {})
    suppress = _read_json(SUPPRESS_PATH, {})
    if not isinstance(seen, dict):
        seen = {}
    if not isinstance(suppress, dict):
        suppress = {}

    dead = 0
    stalled = 0
    running = 0
    park_reasons = ""

    for job, entry in distinct_running_jobs():
        pid = int(entry.get("pid") or 0)
        slot_dir = entry.get("dir") or ""
        started = entry.get("started") or ""
        exp_id = job.split("-", 1)[0]
        started_epoch = parse_started(started) if started else now
        seen_pid = int(seen.get(job, 0) or 0)

        if pid_alive(pid):
            running += 1
            if seen_pid != pid:
                cbq("exec-event", exp_id, "--kind", "launch",
                    "--pid", str(pid), quiet=True)
                jqset(SEEN_PATH, job, pid)
            plog = progress_log_path(slot_dir, job)
            if plog is not None:
                if file_stalled(plog, STALL_SEC):
                    stalled += 1
            elif (now - started_epoch) >= STALL_SEC:
                stalled += 1
        else:
            if seen_pid == pid:
                wall = max(0, now - started_epoch)
                tail_path = Path(f"/tmp/run-{job}.out")
                note = ""
                if tail_path.exists():
                    try:
                        with open(tail_path, "rb") as f:
                            f.seek(0, 2)
                            size = f.tell()
                            f.seek(max(0, size - 300))
                            note = f.read().decode(
                                "utf-8", errors="replace"
                            ).replace("\n", " ").replace("\t", " ")
                    except OSError:
                        pass
                args = ["exec-event", exp_id, "--kind", "death",
                        "--pid", str(pid), "--wall-sec", str(wall)]
                if note:
                    args += ["--note", note]
                cbq(*args, quiet=True)
                jqset(SEEN_PATH, job, None)
                summary = cbq("exec-summary", exp_id, "--json", quiet=True)
                d1h = 0
                if summary.returncode == 0:
                    try:
                        d1h = int(
                            (json.loads(summary.stdout or "{}")
                             .get("deaths_1h", 0)) or 0
                        )
                    except json.JSONDecodeError:
                        pass
                if d1h >= CRASH_LOOP_DEATHS:
                    park_reasons += (
                        f"PARK {exp_id}: crash loop, {d1h} deaths in 1h "
                        f"(cbq exec-summary {exp_id}) — escalate, do not "
                        f"fix; "
                    )
                    jqset(SUPPRESS_PATH, job, now + SUPPRESS_SEC)
            sup_until = int((suppress.get(job, 0)) or 0)
            if now < sup_until and not park_reasons:
                pass
            else:
                dead += 1

    # Prune SEEN for jobs no longer in slots.
    live_jobs = {j for j, _ in distinct_running_jobs()}
    current_seen = _read_json(SEEN_PATH, {})
    if isinstance(current_seen, dict):
        for stale in [k for k in current_seen if k not in live_jobs]:
            jqset(SEEN_PATH, stale, None)

    return dead, stalled, running, park_reasons


def count_orphan_executing() -> int:
    """``executing`` rows claimed by us whose job holds no slot entry."""
    res = cbq("list", "--status", "executing", "--claimed-by", WORKER_ACTOR,
              "--json", quiet=True)
    if res.returncode != 0:
        return 0
    try:
        rows = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        return 0
    orphans = 0
    slots = _read_json(SLOTS_PATH, {})
    for row in rows:
        job_id = row.get("id") or ""
        if not job_id:
            continue
        in_slots = sum(
            1 for entry in (slots or {}).values()
            if isinstance(entry, dict)
            and isinstance(entry.get("job"), str)
            and entry["job"].startswith(job_id + "-")
        )
        if in_slots == 0:
            orphans += 1
    return orphans


def main(argv: Optional[list[str]] = None) -> int:
    return run_loop() or 0


if __name__ == "__main__":
    sys.exit(main())
