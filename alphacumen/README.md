# alphacumen

AlphaCumen is the finance instance of the [Coral harness](../harness). A planner + specialist swarm over SEC filings, market data, and news, with 70 domain skills carrying the finance conventions.

**Open code, hosted runtime.** The framework, the orchestration, the prompts, the skills — all open. The kernel retrieval verbs that read the prefab finance corpus (BM25 over SEC filings, ANN over scraped articles, SQL over equity bars, multihop graph over the knowledge graph) are stubbed in this repo; the hosted Coral Bricks runtime supplies the real backends + the ~4.5 TB pre-processed corpus.

## Want it hosted?

Talk to the Coral Bricks team for production-grade AlphaCumen runs over the prefab finance corpus — that's how you reproduce the benchmark numbers in the blog post.

→ **[coralbricks.ai/alphacumen](https://coralbricks.ai/alphacumen)**

## Want to fork and BYO data?

Read the code top-down — `swarm.py` → a specialist `persona_file` → `skills/<slug>/` — and replace the kernel-verb stubs in [`harness/stubs/`](../harness/stubs) with calls against your own backend (OpenSearch / Pinecone / DuckDB / your graph DB / your Python sandbox). The framework primitives and the finance conventions transfer; only the data plane is yours.

## What's in here

- `swarm.py` — the orchestrator: planner LLM call + parallel specialist fan-out per round, accumulates the common thread, calls the postprocessor on convergence
- `tools.py` — finance-specific tool wrappers (BM25 SEC, equity bars, compute_technicals, get_full_text, …). Calls into the kernel-verb stubs by default.
- `memo.py` — memo persistence (stubbed in OSS; no cross-call memory)
- `roster.py` — `SpecialistConfig` per finance role (sector / stock / risk / news_quant / vc analyst) + persona prompt loader
- `postprocessor.py` — terminal synthesis call that reads the converged common thread and writes the structured `final_answer`
- `prompts/` — persona system prompts + planner seed + postprocessor template
- `skills/` — finance skills (`compute_*` for math-bound conventions, `extract_*` for retrieval-shaped extraction)
- `planner_skills/` — cross-specialist routing playbooks (fiscal-period resolution, vocabulary mapping, dispatch routing)
- `planner/` — planner-side prompt variants
- `capabilities.py`, `skill_registry.py`, `_langfuse.py`, `index_map.py` — registry + observability glue

## Dependency on `harness/`

`alphacumen` imports from `harness/` for the ReAct loop, skill primitives, constraints, and the LLM client. They ship as one wheel (`cb-ia`) for now; nothing prevents `harness/` from being installed alone if you're building a non-finance harness.

## Benchmarks + example

The benchmark queries (Vals AI Finance Agent v2, FinanceBench) and the in-process example runner live in [`alphacumen-finance-benchmarks/`](../alphacumen-finance-benchmarks). Start with `examples/ask_alphacumen.py`.

## License

Apache 2.0 — see [LICENSE](../LICENSE) at the repo root.

## Read more

Blog post: [Write Your Own Agent Harness](https://coralbricks.ai/blog/write-your-own-harness)
