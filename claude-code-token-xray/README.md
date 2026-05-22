# claude-code-token-xray

Reverse-engineer a month of your own local Claude Code logs
(`~/.claude/projects/*/*.jsonl`) into where the **tokens, time, and cost**
actually go. This is the complete tooling behind the blog post *"Claude Code felt
slow. So I X-rayed a month of my own logs."*

Everything reads **only local logs** — nothing is sent anywhere.

```bash
pip install -r requirements.txt   # just tiktoken
python3 token_time_breakdown.py
python3 cost.py
python3 main_vs_sidecar.py
```

> tiktoken is OpenAI's tokenizer, not Claude's, so token *proportions* are
> reliable to ~±15%, not Claude-exact. The billed-token counts in `cost.py` come
> straight from the API `usage` blocks and are exact.

## Scripts

- **`token_time_breakdown.py`** — the headline table: tokens (marked input/output)
  **and** wall-clock time per activity (reasoning, running commands, writing tool
  calls, subagents, summaries, reading/searching, editing) plus the
  passive-context rows (system prompt + tools, attachments, the typed prompt,
  injected reminders). One pass, so tokens and time stay consistent. Reasoning
  isn't stored in plaintext (only an encrypted signature), so it's recovered by
  subtraction: `output − tool_calls − summaries`. Time is reconstructed from
  event timestamps.
- **`cost.py`** — billed token totals (cache reads / cache writes by TTL / fresh
  input / output) priced at Opus 4.7 list rates, plus the no-caching
  counterfactual.
- **`main_vs_sidecar.py`** — splits the human-driven main thread from spawned
  subagents (logged under nested `*/subagents/*.jsonl`); reports billed tokens,
  per-model mix, cache-hit rate, and cost for each, plus the combined total.

## Caveats

- One person's month on one machine — directional, not a benchmark. Claude Code
  is dynamic, so your split will differ. That's the point: run it on yours.
- A generation-time gap also includes the model reading its context before it
  writes; Bash time is real execution (commands auto-approved), but code run in
  the background or a separate terminal isn't counted.
- The system-prompt row is estimated from each session's first cache write.

## License

Apache 2.0 — see the repository [LICENSE](../LICENSE).
