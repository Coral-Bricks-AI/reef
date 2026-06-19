# AlphaCumen finance benchmarks

The benchmark queries and in-process runner for [`alphacumen/`](../alphacumen) — the worked finance instance of the [Coral harness](../harness).

The numbers in the [blog write-up](https://coralbricks.ai/blog/write-your-own-harness) (82.6% on Vals AI Finance Agent v2 with Kimi K2.6, vs. 44.87% on the reference harness with the same model) come from running this code against the prefab finance corpus via the hosted runtime.

## Run the in-process example

[`examples/ask_alphacumen.py`](examples/ask_alphacumen.py) shows the in-process API end-to-end. Out of the box (no backend wired), it raises `NotImplementedError` from the first retrieval call with a redirect message — that's the demo, not a bug.

```bash
git clone https://github.com/Coral-Bricks-AI/coral-ai.git
cd coral-ai
export OPENAI_API_KEY=sk-...
python alphacumen-finance-benchmarks/examples/ask_alphacumen.py
```

To get a real answer back you need a retrieval backend. Two paths:

**Hosted experience** — Coral Bricks runs AlphaCumen over a ~4.5 TB pre-processed finance corpus (SEC filings, equity bars, news, knowledge graph). Talk to the team at [coralbricks.ai/alphacumen](https://coralbricks.ai/alphacumen).

**BYO data** — Replace the kernel-verb stubs in [`harness/stubs/`](../harness/stubs) with calls against your own backend (OpenSearch / Pinecone / DuckDB / your graph DB / your Python sandbox).

## Reproduce the benchmarks

The 50-question public Vals AI slice lives at [`benchmark_queries_50.md`](benchmark_queries_50.md). Loop the example over each row and score against the [Vals AI](https://huggingface.co/datasets/vals-ai/finance_agent_benchmark) / [FinanceBench](https://huggingface.co/datasets/PatronusAI/financebench) gold keys.

## License

Apache 2.0 — see [LICENSE](../LICENSE) at the repo root.
