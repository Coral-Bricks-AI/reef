# Vals AI Finance Agent benchmark (v1.1) eval

Reproducible eval of hosted AlphaCumen against the public 50-question
slice of the [Vals AI Finance Agent benchmark](https://huggingface.co/datasets/vals-ai/finance_agent_benchmark).

The benchmark is the same one Anthropic cites in the Opus 4.7 system
card (64.4%, 1st on leaderboard at the time). The public 50 are
CC-BY-4.0; the other 487 questions stay with Vals AI.

> **Vals AI v2** (the 82.6% headline number) isn't shipped in this
> round — the v2 reference harness has a gated license and our
> vendored copy lives behind that. The public v2 dataset is on Vals
> AI's site; the runner shape mirrors `eval_one.py` exactly, so
> wiring the v2 CSV against the same `runner.ask_alphacumen_hosted`
> call is straightforward.

## How it works

1. Pull the question from `data/finance_agent_benchmark.csv`.
2. Submit it to the **hosted AlphaCumen pipeline** via the Coral platform
   gateway (`alphacumen.evals.common.runner.ask_alphacumen_hosted`).
3. Take `result_json["answer_summary"]` as the candidate answer.
4. Each rubric atom (correctness / contradiction) is graded by Claude
   independently. Final verdict = all correctness atoms pass AND no
   contradiction atom fails.

## Auth

| Service | Where the key is read |
|---|---|
| Coral platform | `$CORAL_API_KEY` env var, or `~/.coral/api_key` file |
| Anthropic (judge) | `$ANTHROPIC_API_KEY` env var |

Gateway URL defaults to the public production gateway; override with
`$CORAL_PLATFORM_URL`.

## Run on one question

```bash
export CORAL_API_KEY=ak_...
export ANTHROPIC_API_KEY=sk-ant-...

python -m alphacumen.evals.valsai.eval_one
```

Pick a different question and save the full envelope:

```bash
python -m alphacumen.evals.valsai.eval_one --row 5 --out /tmp/row5.json
```

Sweep all 50 rows:

```bash
python -m alphacumen.evals.valsai.eval_one --all --json-out /tmp/vals.jsonl
```

Pin the run to a date (`--asof` uses `mode=backtest` so the pipeline
treats this as "today"; useful when reference answers target a specific
fiscal year):

```bash
python -m alphacumen.evals.valsai.eval_one --all --asof 2025-03-01T00:00:00Z
```

Debug judge logic without a hosted submission (paste an answer to stdin):

```bash
echo "Elinor Mertz" | python -m alphacumen.evals.valsai.eval_one --row 0 --skip-coral
```

## Knobs

| Env / flag | Default | What |
|---|---|---|
| `VALSAI_JUDGE_MODEL` | `claude-sonnet-4-6` | Anthropic model for the rubric grader |
| `VALSAI_HARNESS_MODEL` | `lilac/moonshotai/kimi-k2.6` | LLM driven by the hosted AlphaCumen pipeline |
| `--pipeline-package` | `cb-ia==latest` | Pin to an exact version (e.g. `cb-ia==0.0.420`) for repeatable runs |
| `--asof` | none | Pin the run to an ISO-8601 UTC date via `mode=backtest` |
| `CORAL_PLATFORM_URL` | public prod gateway | Override for staging / private deployments |
| `--json-out` | off | Append each row's full envelope as JSONL to PATH |

The runner ships a handful of per-row `--asof` overrides for rows whose
reference filings sit just past the global clamp (see
`_PER_ROW_ASOF_OVERRIDES` in `eval_one.py`).

## Output

```
=== Row 0 | Qualitative Retrieval | 9 rubric atoms ===
Q: In 2024, who was Nominated to Serve on BBSI's Board of Directors?
Reference A: Thomas Carley | ... | Vincent Price

[step 1/2] asking hosted AlphaCumen...
[coral] submitted request_id=...
[step 2/2] grading with Claude (claude-sonnet-4-6) ...
  [judge] atom 1/9 correctness PASS: Thomas Carley
  ...

=== Row 0 verdict ===
correctness atoms passed: 8/8
contradiction atoms failed: 0/1
partial credit: 100.0%
fully correct (strict): True
```

## Limitations

- Only 50 of 537 questions are public. Use this for plumbing checks and
  diagnostics, not statistical claims about Coral vs. the leaderboard.
- The judge is a single Claude model (Sonnet 4.6 by default). For
  publication-grade numbers, run a multi-model jury and majority-vote
  per atom — the per-atom call shape is already the right unit for that.
- Some questions reference live state ("current CFO"). If the hosted
  pipeline's ingestion is older than the dataset's reference date, those
  will fail for ingestion-staleness reasons unrelated to retrieval
  quality. Filter by `Question Type` or use `--asof` to anchor.

## Files

- `eval_one.py` — single-question / sweep end-to-end runner.
- `data/finance_agent_benchmark.csv` — public 50, CC-BY-4.0, fetched
  from `huggingface.co/datasets/vals-ai/finance_agent_benchmark`.
- shared plumbing: [`../common/`](../common) (see [`../README.md`](../README.md)).
