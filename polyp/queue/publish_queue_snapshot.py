#!/usr/bin/env python3
"""Publish a cb_queue row snapshot to CloudWatch Logs (group /cb/architect/queue).

Run from cron every ~1-2 minutes (architect box). Feeds the dashboard's
"Queue snapshot" table widget, which renders the CURRENT state of every
experiment row, most-recently-updated first.

Design — why a full snapshot stamped at *now*, not at each row's updated_at:
- A CloudWatch dashboard log widget only scans its time window (the dashboard's
  default is the last 1h). If we stamped each event with the row's updated_at,
  a row that last changed yesterday would fall outside a 1h window and vanish —
  so the table wouldn't be a snapshot, only "rows touched in the window".
- Instead we re-emit EVERY current row each cycle with the event timestamp set
  to publish time (now). So any window >= one cron interval contains the whole
  current table exactly once per row per cycle. The widget collapses the
  repeats and orders by real update time with:

      SOURCE '/cb/architect/queue'
        | fields id, status, kind, machine, slug, claimed_by, last_error, updated_at
        | sort updated_ms desc | dedup id

  `updated_ms` (epoch-ms of updated_at, carried in the JSON) is the sort key —
  numeric, unambiguous, and independent of the event timestamp. `dedup` keeps
  the first row per id in the sorted input (so the newest update per id) and
  preserves that order; it MUST stay terminal (a trailing sort is a
  MalformedQueryException), which is fine — the sort already ran.

Scope: all non-terminal rows (the live + parked queue) plus terminal rows
updated within TERMINAL_RECENT_HOURS, so recent done/falsified results stay
visible without replaying the whole archive every minute.

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

PACIFIC = ZoneInfo("America/Los_Angeles")  # display updated_at in PT (PST/PDT)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cbq  # noqa: E402

REGION = "us-east-1"
LOG_GROUP = "/cb/architect/queue"
LOG_STREAM = "snapshot"
TERMINAL_RECENT_HOURS = 24  # keep recently-finished rows in the snapshot
MAX_ERR = 160               # truncate last_error so the table column stays readable


def _aws(*args: str, **kw):
    return subprocess.run(["aws", "--region", REGION, *args],
                          capture_output=True, text=True, timeout=60, **kw)


def ensure_stream():
    _aws("logs", "create-log-stream", "--log-group-name", LOG_GROUP,
         "--log-stream-name", LOG_STREAM)  # no check: ResourceAlreadyExists is fine


def put_events(events: list[dict]):
    """All events share the publish timestamp, so a batch never spans >24h;
    only the 1000-events/1MB-per-call limit applies."""
    events.sort(key=lambda e: e["timestamp"])
    for i in range(0, len(events), 1000):
        batch = events[i:i + 1000]
        r = _aws("logs", "put-log-events", "--log-group-name", LOG_GROUP,
                 "--log-stream-name", LOG_STREAM, "--log-events", json.dumps(batch))
        if r.returncode != 0:
            # Modern PutLogEvents needs no sequence token; if this account still
            # enforces one, parse the expected token from the error and retry.
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
            "SELECT id, status, kind, machine, slug, origin, claimed_by,"
            "       left(coalesce(last_error, ''), %s) AS last_error,"
            "       updated_at,"
            "       (extract(epoch from updated_at) * 1000)::bigint AS updated_ms"
            "  FROM cb_queue.experiments"
            " WHERE status <> ALL(%s)"
            "    OR updated_at > now() - make_interval(hours => %s)"
            " ORDER BY updated_at DESC",
            (MAX_ERR, list(cbq.TERMINAL_STATUSES), TERMINAL_RECENT_HOURS),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("queue empty; nothing to publish")
        return

    ensure_stream()
    now_ms = int(time.time() * 1000)
    events = [{
        "timestamp": now_ms,
        "message": json.dumps({
            "id": r["id"], "status": r["status"], "kind": r["kind"],
            "machine": r["machine"], "slug": r["slug"], "origin": r["origin"],
            "claimed_by": r["claimed_by"] or "-",
            "last_error": r["last_error"] or "",
            # Wall-clock in Pacific (PST/PDT) for the table; updated_ms (epoch,
            # tz-independent) stays the sort key so ordering is unaffected.
            "updated_at": r["updated_at"].astimezone(PACIFIC).strftime("%Y-%m-%d %H:%M:%S %Z"),
            "updated_ms": int(r["updated_ms"]),
        }),
    } for r in rows]
    put_events(events)
    print(f"published snapshot of {len(rows)} row(s)")


if __name__ == "__main__":
    main()
