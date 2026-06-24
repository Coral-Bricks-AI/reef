# AlphaCumen evals

Reproducible benchmark runners for the published AlphaCumen finance
numbers: **82.6%** on Vals AI Finance Agent v2, **90%** on Vals AI v1.1,
**89.3%** on Patronus FinanceBench — at ~**$0.13/query**.

> **These evals submit to the hosted AlphaCumen pipeline on the Coral
> platform.** The agent code under [`alphacumen/`](..) is open source and
> standalone, but its retrieval verbs (`bm25`, `ann`, `sql`, `multihop`,
> `get`, `py`) are `NotImplementedError` stubs in the OSS clone — the
> hosted runtime swaps these for the real backends (SEC filings, GDELT,
> macro). Submitting to hosted lets you reproduce the published numbers
> exactly. Wiring your own retrieval against your own data is also
> supported; see [`reef/stubs/`](../../reef/stubs).

## Folder layout

```
alphacumen/evals/
├── README.md                       (this file)
├── common/
│   ├── config.py                   defaults, gateway URL, SSL ctx, API-key reader
│   ├── runner.py                   urllib client → hosted Coral gateway (submit + poll)
│   ├── judge.py                    Anthropic judge transport
│   ├── cli.py                      shared --json-out argparse
│   └── util.py                     row selectors, JSONL append, field pickers
├── valsai/
│   ├── eval_one.py                 Vals AI Finance Agent v1.1 — per-atom rubric grader
│   ├── data/finance_agent_benchmark.csv     (public 50, CC-BY-4.0)
│   └── README.md
└── financebench/
    ├── eval_one.py                 Patronus FinanceBench — single-verdict grader
    ├── data/financebench_open_source.jsonl  (public 150, CC-BY-NC-4.0)
    └── README.md
```

Vals AI Finance Agent **v2** (the 82.6% number) isn't open-sourced in
this round — the Vals AI v2 reference harness ships with a gated
license, and our internal copy is vendored under those terms. The
public dataset itself is available on Vals AI's site; the runner shape
mirrors `valsai/eval_one.py` exactly, so wiring the v2 dataset against
the same `runner.ask_alphacumen_hosted` call is straightforward.

## Auth

| Service | How to set it |
|---|---|
| **Coral platform** (hosted submit) | `$CORAL_API_KEY` env var (raw `ak_...` value), or `$CORAL_API_KEY_FILE`, or a `~/.coral/api_key` file. Sign up / get a key at <https://coralbricks.ai/alphacumen>. |
| **Anthropic** (judge) | `$ANTHROPIC_API_KEY` env var (raw `sk-ant-...` value). |
| Coral gateway URL | Defaults to the public production gateway. Override with `$CORAL_PLATFORM_URL` for staging / private deployments. |

## Quickstart

Single FinanceBench row:

```bash
export CORAL_API_KEY=ak_...
export ANTHROPIC_API_KEY=sk-ant-...

python -m alphacumen.evals.financebench.eval_one --row 0 --out /tmp/fb_row0.json
```

Sweep all 150 FinanceBench rows, log each to a JSONL:

```bash
python -m alphacumen.evals.financebench.eval_one --all --json-out /tmp/fb.jsonl
```

Single Vals AI v1.1 row:

```bash
python -m alphacumen.evals.valsai.eval_one --row 0 --out /tmp/vals_row0.json
```

Switch the underlying harness model (passed to the hosted gateway):

```bash
python -m alphacumen.evals.financebench.eval_one --row 0 \
    --model anthropic/claude-sonnet-4-6
```

Pin the pipeline version for repeatable runs across days:

```bash
python -m alphacumen.evals.financebench.eval_one --all \
    --pipeline-package cb-ia==0.0.420
```

## What gets submitted

Each row sends one `POST /v1/batches` to the gateway with:

- `pipeline_package` — defaults to `cb-ia==latest` (the hosted AlphaCumen wheel)
- `model` — the underlying LLM the harness drives (default: `lilac/moonshotai/kimi-k2.6`)
- `inputs[0].query` — the benchmark question
- `inputs[0].indices` — the public-data corpora to retrieve over (see `config.PIPELINE_INDICES`)
- `mode` — `live` by default; `backtest` + `asof` when a row pins a date

The gateway returns a `request_id`; the runner polls `GET /v1/runs/<id>`
every ~3 s until terminal (typical: 60–200 s, slow-MoE rows can take up
to ~20 min).

## Pricing & rate limits

Submitting these evals against hosted AlphaCumen consumes Coral
platform credits. Per-query cost depends on the model — the **$0.13/query**
number is on `lilac/moonshotai/kimi-k2.6`. Frontier-model defaults (e.g.
`anthropic/claude-opus-4-7`) cost ~10× more. A full FinanceBench sweep
on Kimi K2.6 is ~$20; Vals AI v1.1 (50 rows) is ~$7.

The hosted gateway rate-limits per-key; sweeps run sequentially, not in
parallel, by design. If you need higher throughput, contact the team.

## Limitations

- The public Vals/FinanceBench slices are small (50 / 150 rows). They're
  the right size for plumbing checks and directional diagnostics, not
  for statistical claims about model quality across the full benchmarks.
- The judge is a single Claude model (Sonnet 4.6 by default). For
  publication-grade numbers, run a multi-judge jury and majority-vote
  per row / per atom — the per-call shape here is already the right
  unit for that.
- Benchmark rows reference specific filing periods. The hosted pipeline
  is asof-aware (`--asof 2025-03-01T00:00:00Z`) but if a row references
  data the corpus doesn't cover, the row fails for ingestion-coverage
  reasons unrelated to retrieval quality.

## License

Apache 2.0 (this repo). The bundled datasets carry the licenses they
shipped with — see the per-benchmark READMEs.
