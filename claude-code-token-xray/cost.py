"""Cost of a month of Claude Code at Opus 4.7 list API rates (main thread).

Billed token counts come straight from each turn's `usage` block (cache reads,
cache writes split by 1h/5m TTL, fresh input, output) — nothing is estimated.
Also prints the no-caching counterfactual. Reads only local logs.

Run: python3 cost.py
"""
import json, glob, os, collections

T = collections.Counter()
for f in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
    for line in open(f):
        try: o = json.loads(line)
        except: continue
        m = o.get("message") or {}
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        u = m.get("usage") or {}
        T["cache_read"] += u.get("cache_read_input_tokens", 0) or 0
        T["uncached"]   += u.get("input_tokens", 0) or 0
        T["output"]     += u.get("output_tokens", 0) or 0
        cc = u.get("cache_creation") or {}
        T["write_1h"]   += cc.get("ephemeral_1h_input_tokens", 0) or 0
        T["write_5m"]   += cc.get("ephemeral_5m_input_tokens", 0) or 0

M = 1_000_000
rates  = {"uncached": 5.0, "cache_read": 0.50, "write_5m": 6.25, "write_1h": 10.0, "output": 25.0}  # $/Mtok
labels = {"cache_read": "cache reads", "write_1h": "cache writes (1h)", "write_5m": "cache writes (5m)",
          "uncached": "fresh (uncached) input", "output": "output (incl. reasoning)"}

total = 0.0
for k in ["cache_read", "write_1h", "write_5m", "uncached", "output"]:
    c = T[k] / M * rates[k]; total += c
    print(f"{labels[k]:26s} {T[k]:14,} tok  ${c:9,.2f}")
print(f"{'TOTAL':26s} {sum(T.values()):14,} tok  ${total:9,.2f}")

inp = T["cache_read"] + T["write_1h"] + T["write_5m"] + T["uncached"]
print(f"\nif there were NO caching (all input at full $5/M): "
      f"${inp / M * 5 + T['output'] / M * 25:,.2f}")
