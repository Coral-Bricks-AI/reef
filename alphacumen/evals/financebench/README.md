# FinanceBench eval

Reproducible eval of hosted AlphaCumen against the public 150-question
open-source slice of [FinanceBench](https://github.com/patronus-ai/financebench)
(Islam et al. 2023, Patronus AI — *"FinanceBench: A New Benchmark for
Financial Question Answering"*).

FinanceBench is a retrieval-over-SEC-filings benchmark. Each question
is scoped to a specific 10-K / 10-Q / 8-K and has a single human-verified
gold answer plus a justification. The full set is 10,231 questions;
only the 150 open-source ones (CC-BY-NC-4.0) ship publicly — the rest
stay with Patronus.

## How it works

1. Pull the question from `data/financebench_open_source.jsonl`.
2. Submit it to the **hosted AlphaCumen pipeline** via the Coral platform
   gateway (`alphacumen.evals.common.runner.ask_alphacumen_hosted`).
3. Take `result_json["answer_summary"]` as the candidate answer.
4. Claude grades the candidate against the gold answer and returns one
   verdict: `correct` / `incorrect` / `refused` (mirrors FinanceBench's
   own correct/incorrect/refused taxonomy). Numerical equivalence and
   unit restatements (`$1,577M` ≡ `1577.00` ≡ `$1.577 billion`) count
   as correct.

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

python -m alphacumen.evals.financebench.eval_one
```

Pick a different question and save the full envelope:

```bash
python -m alphacumen.evals.financebench.eval_one --row 5 --out /tmp/row5.json
```

Sweep a range (each row writes `row_<N>.json` into the `--out` dir):

```bash
python -m alphacumen.evals.financebench.eval_one --rows 0-49 --out /tmp/fb
python -m alphacumen.evals.financebench.eval_one --all --out /tmp/fb
```

Append every row's envelope to a single JSONL log:

```bash
python -m alphacumen.evals.financebench.eval_one --all --json-out /tmp/fb.jsonl
```

Debug judge logic without a hosted submission (paste an answer to stdin):

```bash
echo '$1,577 million' | python -m alphacumen.evals.financebench.eval_one --row 0 --skip-coral
```

## Knobs

| Env / flag | Default | What |
|---|---|---|
| `FINANCEBENCH_JUDGE_MODEL` | `claude-sonnet-4-6` | Anthropic model for the grader |
| `FINANCEBENCH_HARNESS_MODEL` | `lilac/moonshotai/kimi-k2.6` | LLM driven by the hosted AlphaCumen pipeline |
| `--pipeline-package` | `cb-ia==latest` | Pin to an exact version (e.g. `cb-ia==0.0.420`) for repeatable runs |
| `CORAL_PLATFORM_URL` | public prod gateway | Override for staging / private deployments |
| `--json-out` | off | Append each row's full envelope as JSONL to PATH |
| `--query-suffix` | "" | Text appended to the question before sending (judge sees the original) |

## Output

```
=== Row 0 | financebench_id_03029 | metrics-generated | 3M / 3M_2018_10K ===
Q: What is the FY2018 capital expenditure amount (in USD millions) for 3M? ...
Gold A:
$1577.00

[step 1/2] asking hosted AlphaCumen...
[coral] submitted request_id=...
[step 2/2] grading with Claude (claude-sonnet-4-6) ...
  [judge] verdict=CORRECT: candidate states $1,577M for FY2018 capex, matching the gold answer

=== Row 0 verdict ===
verdict: correct
reason:  candidate states $1,577M for FY2018 capex, matching the gold answer
correct: True
```

## Limitations

- Only 150 of 10,231 questions are public. Use this for plumbing checks
  and diagnostics, not statistical claims about the full benchmark.
- The judge is a single model (Sonnet 4.6 by default). For
  publication-grade numbers, run a multi-model jury and majority-vote
  the verdict — the per-row call shape is the right unit for that.
- FinanceBench questions are anchored to a specific filing period
  ("FY2018", "as of the 2022 10-K"). If the hosted pipeline's
  ingestion doesn't cover that filing, the row fails for
  ingestion-coverage reasons unrelated to retrieval quality.

## Files

- `eval_one.py` — single-question / sweep end-to-end runner.
- `data/financebench_open_source.jsonl` — public 150, CC-BY-NC-4.0,
  fetched from `github.com/patronus-ai/financebench`
  (`data/financebench_open_source.jsonl`).
- shared plumbing: [`../common/`](../common) (see [`../README.md`](../README.md)).
