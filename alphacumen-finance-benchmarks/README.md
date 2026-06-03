# AlphaCumen finance benchmarks

The swarm layer behind AlphaCumen — many finance specialists running in parallel over a private corpus. The pattern behind our benchmark results: **96% on Vals AI v1, 99.3% on FinanceBench, $0.33 per question on open models.** [Full write-up](https://coralbricks.ai/blog/alphacumen-finance-benchmarks).

## Install

```bash
pip install coralbricks-cli
```

> `coralbricks-cli` is in design-partner preview today; the public install path opens in Phase 2.

## API key

Get a `CORAL_API_KEY` from [coralbricks.ai/alphacumen](https://coralbricks.ai/alphacumen) and export it:

```bash
export CORAL_API_KEY=ak_...
```

## Run a single query

[`examples/ask_alphacumen.py`](examples/ask_alphacumen.py) is the minimum submit-and-poll path the eval harness uses to score both benchmarks. Read its docstring for the full setup.

## Reproduce the benchmarks

The 50-question public Vals AI slice lives at [`benchmark_queries_50.md`](benchmark_queries_50.md). Loop `ask_alphacumen.ask()` over each row and score against the [Vals AI](https://huggingface.co/datasets/vals-ai/finance_agent_benchmark) / [FinanceBench](https://huggingface.co/datasets/PatronusAI/financebench) gold keys.

## License

Apache 2.0 — see [LICENSE](../LICENSE) at the repo root.
