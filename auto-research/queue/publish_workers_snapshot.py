#!/usr/bin/env python3
"""Publish a cb_queue.workers heartbeat snapshot to CloudWatch Logs
(group /cb/architect/workers). Feeds the dashboard's "Workers — last monitor
status" table.

Same design as publish_queue_snapshot.py: re-emit every worker row each cycle
stamped at publish time, so any dashboard window >= the cron interval holds the
whole fleet; the widget collapses the per-cycle repeats with

    SOURCE '/cb/architect/workers'
      | fields actor, kind, machine, gpus, last_seen, age_min, status
      | sort seen_ms desc | dedup actor

`seen_ms` (epoch-ms of last_seen) is the sort key — most-recently-seen worker
first, so the live one floats to the top. For an active worker last_seen moves
every heartbeat, so the newest cycle has the largest seen_ms and `dedup` keeps
its fresh age/status; for an offline worker seen_ms is frozen, so dedup may keep
any cycle's row — harmless, its age is large either way. `dedup` must stay
terminal.

`status` is derived from heartbeat age: a live worker's watchdog subloop upserts
its row about every 60s, so active < 5m, stale < 1h, else offline. `last_seen`
is rendered in Pacific (PST/PDT) for the table.

Uses the same DSN resolution as cbq. IAM: logs:CreateLogStream /
logs:PutLogEvents via the box's CloudWatchAgentServerPolicy.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cbq  # noqa: E402

REGION = "us-east-1"
LOG_GROUP = "/cb/architect/workers"
LOG_STREAM = "snapshot"
PACIFIC = ZoneInfo("America/Los_Angeles")
ACTIVE_SEC = 5 * 60     # heartbeat subloop is ~60s; fresh within 5m = active
STALE_SEC = 60 * 60     # within 1h = stale, older = offline


def _aws(*args: str, **kw):
    return subprocess.run(["aws", "--region", REGION, *args],
                          capture_output=True, text=True, timeout=60, **kw)


def ensure_stream():
    _aws("logs", "create-log-stream", "--log-group-name", LOG_GROUP,
         "--log-stream-name", LOG_STREAM)  # no check: ResourceAlreadyExists is fine


def status_for(age_sec: int) -> str:
    if age_sec < ACTIVE_SEC:
        return "active"
    if age_sec < STALE_SEC:
        return "stale"
    return "offline"


def put_events(events: list[dict]):
    """All events share the publish timestamp, so a batch never spans >24h;
    only the 1000-events/1MB-per-call limit applies."""
    events.sort(key=lambda e: e["timestamp"])
    for i in range(0, len(events), 1000):
        batch = events[i:i + 1000]
        r = _aws("logs", "put-log-events", "--log-group-name", LOG_GROUP,
                 "--log-stream-name", LOG_STREAM, "--log-events", json.dumps(batch))
        if r.returncode != 0:
            tok = None
            if "expectedSequenceToken" in r.stderr:
                tok = r.stderr.split("expectedSequenceToken")[1].strip(' :"\n')
            if tok:
                _aws("logs", "put-log-events", "--log-group-name", LOG_GROUP,
                     "--log-stream-name", LOG_STREAM, "--log-events", json.dumps(batch),
                     "--sequence-token", tok, check=True)
            else:
                raise RuntimeError(f"put-log-events failed: {r.stderr.strip()}")


def main():
    conn = cbq.connect()
    try:
        rows = conn.execute(
            "SELECT actor, kind, machine, gpus, last_seen,"
            "       extract(epoch FROM now() - last_seen)::int AS age_sec"
            "  FROM cb_queue.workers ORDER BY last_seen DESC"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("no workers registered; nothing to publish")
        return

    ensure_stream()
    now_ms = int(time.time() * 1000)
    events = [{
        "timestamp": now_ms,
        "message": json.dumps({
            "actor": r["actor"], "kind": r["kind"] or "any",
            "machine": r["machine"], "gpus": r["gpus"] or "-",
            "last_seen": r["last_seen"].astimezone(PACIFIC).strftime("%Y-%m-%d %H:%M:%S %Z"),
            "seen_ms": int(r["last_seen"].timestamp() * 1000),
            "age_min": round(r["age_sec"] / 60, 1),
            "status": status_for(r["age_sec"]),
        }),
    } for r in rows]
    put_events(events)
    print(f"published snapshot of {len(rows)} worker(s)")


if __name__ == "__main__":
    main()
