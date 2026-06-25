#!/usr/bin/env python3
"""Publish cb_queue flow + depth metrics to CloudWatch (namespace CBQueue).

Run from cron every ~5 minutes (architect box). Two metric families:

- Flow  (dimension Stage=<to_status>): one count per transition INTO a status,
  emitted with the transition's real timestamp — so a dashboard widget with
  stat=Sum, period=3600 shows exact per-hour flow through every stage
  regardless of publisher cadence. A seq watermark (state file) makes each
  transition publish exactly once.
- Depth (dimension Stage=<status>): current row count per non-terminal status,
  a gauge sampled at publish time (done/falsified/cancelled grow forever and
  would just be a counter; flow already covers them). Also emitted per
  Stage+Kind, per Stage+MachineType (a10/h100), and per
  Stage+MachineType+Kind (the full grid, e.g. h100/finetune vs
  h100/loadtest) — all zero-filled so lines stay continuous.

Uses the same DSN resolution as cbq. IAM: PutMetricData via the boxes'
CloudWatchAgentServerPolicy.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cbq  # noqa: E402

NAMESPACE = "CBQueue"
STATE_FILE = Path.home() / ".cw_flow_seq"
DEPTH_STATUSES = cbq.LIVE_STATUSES + cbq.PARKED_STATUSES
# emit-once backstop: never re-publish datapoints older than CloudWatch accepts
MAX_BACKFILL_HOURS = 24


def put_metric_data(metric_data: list[dict]):
    for i in range(0, len(metric_data), 20):  # CLI caps at 20 datapoints/call
        subprocess.run(
            ["aws", "cloudwatch", "put-metric-data", "--namespace", NAMESPACE,
             "--metric-data", json.dumps(metric_data[i:i + 20])],
            check=True, capture_output=True, text=True, timeout=60,
        )


def main():
    last_seq = 0
    if STATE_FILE.is_file():
        try:
            last_seq = int(STATE_FILE.read_text().strip())
        except ValueError:
            pass

    conn = cbq.connect()
    try:
        # join experiments for the kind so flow can be split per kind.
        rows = conn.execute(
            "SELECT t.seq, t.to_status, e.kind,"
            "       date_trunc('minute', t.created_at) AS minute"
            " FROM cb_queue.transitions t"
            " JOIN cb_queue.experiments e ON e.id = t.experiment_id"
            " WHERE t.seq > %s AND t.created_at > now() - make_interval(hours => %s)"
            " ORDER BY t.seq",
            (last_seq, MAX_BACKFILL_HOURS),
        ).fetchall()
        depth_rows = conn.execute(
            "SELECT status, kind, machine, count(*) AS n FROM cb_queue.experiments"
            " WHERE status = ANY(%s) GROUP BY status, kind, machine",
            (list(DEPTH_STATUSES),),
        ).fetchall()
    finally:
        conn.close()

    data = []
    # Flow: aggregate (Stage only — back-compat with the existing widget) AND
    # per-kind (Stage+Kind) so the loadtest kind is chartable on its own.
    flow_agg = Counter((r["to_status"], r["minute"]) for r in rows)
    flow_kind = Counter((r["to_status"], r["kind"], r["minute"]) for r in rows)
    for (stage, minute), n in sorted(flow_agg.items(), key=lambda kv: kv[0][1]):
        data.append({"MetricName": "Flow",
                     "Dimensions": [{"Name": "Stage", "Value": stage}],
                     "Timestamp": minute.isoformat(), "Value": n, "Unit": "Count"})
    for (stage, kind, minute), n in sorted(flow_kind.items(), key=lambda kv: kv[0][2]):
        data.append({"MetricName": "Flow",
                     "Dimensions": [{"Name": "Stage", "Value": stage}, {"Name": "Kind", "Value": kind}],
                     "Timestamp": minute.isoformat(), "Value": n, "Unit": "Count"})
    # Depth: per status + (status, kind) + (status, machine) + (status, machine, kind).
    depth_agg: Counter = Counter()
    depth_kind: Counter = Counter()
    depth_machine: Counter = Counter()
    depth_machine_kind: Counter = Counter()
    for r in depth_rows:
        depth_agg[r["status"]] += r["n"]
        depth_kind[(r["status"], r["kind"])] += r["n"]
        depth_machine[(r["status"], r["machine"])] += r["n"]
        depth_machine_kind[(r["status"], r["machine"], r["kind"])] += r["n"]
    for status in DEPTH_STATUSES:
        data.append({"MetricName": "Depth",
                     "Dimensions": [{"Name": "Stage", "Value": status}],
                     "Value": depth_agg.get(status, 0), "Unit": "Count"})
    for (status, kind), n in sorted(depth_kind.items()):
        data.append({"MetricName": "Depth",
                     "Dimensions": [{"Name": "Stage", "Value": status}, {"Name": "Kind", "Value": kind}],
                     "Value": n, "Unit": "Count"})
    for status in DEPTH_STATUSES:
        for machine in cbq.MACHINES:
            data.append({"MetricName": "Depth",
                         "Dimensions": [{"Name": "Stage", "Value": status}, {"Name": "MachineType", "Value": machine}],
                         "Value": depth_machine.get((status, machine), 0), "Unit": "Count"})
    for status in DEPTH_STATUSES:
        for machine in cbq.MACHINES:
            for kind in cbq.KINDS:
                data.append({"MetricName": "Depth",
                             "Dimensions": [{"Name": "Stage", "Value": status},
                                            {"Name": "MachineType", "Value": machine},
                                            {"Name": "Kind", "Value": kind}],
                             "Value": depth_machine_kind.get((status, machine, kind), 0),
                             "Unit": "Count"})

    put_metric_data(data)
    if rows:
        STATE_FILE.write_text(str(rows[-1]["seq"]))
    print(f"published {len(rows)} flow transition(s) "
          f"(seq>{last_seq}), {len(DEPTH_STATUSES)} depth gauges")


if __name__ == "__main__":
    main()
