#!/usr/bin/env bash
# Snapshot ~/worker/slots.json to ~/worker/slots-cw.log as NDJSON — one line per
# schedulable GPU (busy or free). The CloudWatch agent tails this file into log
# group /cb/worker/slots (stream = {instance_id}), where the dashboard's
# "Experiments running per GPU" Logs Insights table renders it with
#     ... | sort @timestamp desc | dedup instance, gpu
# i.e. the most recent line per physical GPU = what is running there right now.
#
# Run every minute (cron) on each worker box; the writer is the only moving
# part — shipping + retention are the agent's and CloudWatch's job. Standalone,
# idempotent, best-effort: a missing slots.json or a bad read just no-ops.
set -uo pipefail

SLOTS="${SLOTS:-$HOME/worker/slots.json}"
OUT="${SLOTS_CW_LOG:-$HOME/worker/slots-cw.log}"
[ -s "$SLOTS" ] || exit 0

# Instance id via IMDSv2 (best-effort; cached across runs in CB_INSTANCE_ID).
iid="${CB_INSTANCE_ID:-}"
if [ -z "$iid" ]; then
  tok=$(curl -sf -m 2 -X PUT "http://169.254.169.254/latest/api/token" \
        -H "X-aws-ec2-metadata-token-ttl-seconds: 300" 2>/dev/null || true)
  iid=$(curl -sf -m 2 ${tok:+-H "X-aws-ec2-metadata-token: $tok"} \
        http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null || true)
fi
iid="${iid:-unknown}"

now_epoch=$(date +%s)
ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# One JSON object per GPU key. busy => job/exp/slug/pid/age; free => state only.
# slug strips the leading "<id>-" and an optional trailing "-YYYYMMDD-HHMMSS".
jq -c --arg iid "$iid" --arg ts "$ts" --argjson now "$now_epoch" '
  to_entries[] | .key as $gpu | .value as $v |
  if $v == null then
    {ts:$ts, instance:$iid, gpu:$gpu, state:"free",
     exp:"-", slug:"-", job:"-", pid:0, age_min:0, timeout_min:0}
  else
    ($v.job // "") as $job |
    {ts:$ts, instance:$iid, gpu:$gpu, state:"busy",
     exp:($job | split("-")[0]),
     slug:($job | sub("^[0-9.]+-"; "") | sub("-[0-9]{8}-[0-9]{6}$"; "")),
     job:$job, pid:($v.pid // 0),
     age_min:(($now - (($v.started // "") | fromdateiso8601? // $now)) / 60 | floor),
     timeout_min:($v.timeout_min // 0)}
  end
' "$SLOTS" >> "$OUT" 2>/dev/null || exit 0

# Bound the local file — CloudWatch keeps the durable copy (30-day retention),
# so we only need enough tail for the agent to have shipped. A new inode after
# the swap makes the agent re-read; at ~1 write/min the rare duplicate is
# harmless to a dedup-latest query.
lines=$(wc -l < "$OUT" 2>/dev/null || echo 0)
if [ "${lines:-0}" -gt 20000 ]; then
  tmp=$(mktemp) && tail -n 3000 "$OUT" > "$tmp" && mv "$tmp" "$OUT"
fi
