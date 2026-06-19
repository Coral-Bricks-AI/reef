<div align="center">

# AlphaCumen

### State-of-the-art on public financial benchmarks. Lowest cost per query.

**82.6%** on Vals AI Finance Agent v2 &nbsp;·&nbsp; **90%** on Vals AI v1.1 &nbsp;·&nbsp; **89.3%** on FinanceBench &nbsp;·&nbsp; **$0.13** per question

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](../LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](#)
[![Stars](https://img.shields.io/github/stars/Coral-Bricks-AI/coral-ai?style=social)](https://github.com/Coral-Bricks-AI/coral-ai)
[![Blog: finance-benchmarks](https://img.shields.io/badge/blog-finance--benchmarks-orange)](https://coralbricks.ai/blog/finance-benchmarks)
[![Blog: retrieval-vs-full-stack](https://img.shields.io/badge/blog-retrieval--vs--full--stack-orange)](https://coralbricks.ai/blog/coral-retrieval-vs-full-stack)

<img src="assets/alphacumen-three-benchmarks-summary.png" alt="AlphaCumen state-of-the-art results across Vals AI v2 (82.6%), Vals AI v1.1 (90%), and FinanceBench (89.3%) at $0.13 per question — ~10× cheaper than Opus 4.7" width="820">

</div>

---

## What this is

**AlphaCumen is a multi-agent harness** — 7 agents, 69 skills encoding financial conventions, across four datasets (SEC, news, stocks, options) — running on Kimi K2.6.

Swap Vals AI's generic harness for AlphaCumen's finance-specific stack, and the same Kimi K2.6 model **gains 38 points on v2 and 33 points on v1.1**. Frontier models on the generic harness top out in the 44–64% range. AlphaCumen lands at 82.6 / 90 / 89.3.

> **One sentence:** A domain-specific agent harness beats every frontier model on finance benchmarks, at ~10× lower cost — and the code that does it is open.

---

## The headline numbers

| Benchmark | Top frontier (generic harness) | **AlphaCumen** | Gain |
|---|--:|--:|--:|
| Vals AI Finance Agent **v2** (27 questions, 239 atoms) | 57.86% (Gemini 3.5 Flash) | **82.6%** | **+24.7pp** |
| Vals AI Finance Agent **v1.1** (50 questions) | 64.4% (Opus 4.7) | **90.0%** | **+25.6pp** |
| Patronus AI **FinanceBench** (150 questions) | — (no live leaderboard) | **89.3%** | — |

All accuracy numbers reported with 95% CIs in the [full write-up](https://coralbricks.ai/blog/finance-benchmarks).

### Cost — API only, per query, on Vals AI v1.1

| Stack | $ / query | vs Opus 4.7 |
|---|--:|--:|
| Opus 4.7 | $1.348 | 1.0× |
| Kimi K2.6 (vanilla) | $0.205 | 6.6× cheaper |
| **Kimi K2.6 on Coral** | **$0.133** | **10.2× cheaper** |

Two drivers: an open, affordable base model (Kimi K2.6 lists at a fraction of Opus 4.7 per token), and a runtime built for the shape of agent workloads — **multi-call, cache-heavy** traffic that a metered API doesn't pass through. Per query, AlphaCumen runs ~23 turns averaging **619K total tokens**, of which **82.9% are cached input**. Best accuracy and lowest cost in the same system.

---

## The leaderboards

### Vals AI Finance Agent v2 — newest, hardest

<div align="center">

<img src="assets/alphacumen-valsai-v2-leaderboard.svg" alt="Vals AI Finance Agent v2 leaderboard — AlphaCumen with Kimi K2.6 at 82.6%, generic-harness frontier models in 44–58% range" width="820">

</div>

Same model (Kimi K2.6) on the generic Vals AI harness scores **44.87%**. On AlphaCumen's harness: **82.6%**. That delta is the domain stack.

### Vals AI Finance Agent v1.1

<div align="center">

<img src="assets/alphacumen-valsai-leaderboard.svg" alt="Vals AI Finance Agent v1.1 leaderboard — AlphaCumen with Kimi K2.6 at 90%, generic-harness frontier models in 57–64% range" width="820">

</div>

---

## Is it the retrieval, or the rest of the stack?

Natural follow-up to the headline result: how much of the 38-point gain over Vals AI's reference harness is retrieval, and how much is reasoning? We ran three configurations — same model (Kimi K2.6), same data, same judge.

| | LLM harness | Retrieval | Skills + computation |
|---|---|---|---|
| **A. Vals AI reference** | Vals AI | Tavily / EDGAR / HTML parse | calculator + `price_history` |
| **B. Retrieval swap** | Vals AI | **6 AlphaCumen indices** | calculator only |
| **C. Full AlphaCumen** | AlphaCumen | 6 indices | 69 skills + dedicated tools |

<div align="center">

<img src="assets/coral-retrieval-vs-full-stack-bars.svg" alt="Atom-pass rate on Vals AI Finance Agent v2 — Vals reference 44.87%, Vals harness + AlphaCumen retrieval 49.8% (at 4× the Vals reference budget), full AlphaCumen 82.6%" width="820">

</div>

**Retrieval alone closes ~5 of the 38 points — about an eighth of the gap.** And only at **4× the Vals reference budget** (200 turns / 7200s instead of 50 turns / 1800s). At the original budget, configuration B scores *worse* than the reference: **37.2%**, because **132 of 239 atoms never produce a final answer** — Kimi stalls in reasoning prose and hits the wall before reaching `submit_final_result`. Bumping the budget 4× lets 30 more atoms converge — but **70 atoms still time out** even at that budget, and average turns-per-row goes from 10 to 52.

**Then we ran the same 70 hard atoms under configuration C (full AlphaCumen) at the original 1× Vals budget. All 70 converged cleanly.**

<div align="center">

<img src="assets/linkedin-chart.png" alt="Two-panel chart — Panel 1: retrieval swap alone moves Vals reference 44.87% to 49.8% (still 33pp short of AlphaCumen's 82.6%). Panel 2: even at 4× budget, generic harness tops out at 49.8% — bigger budget is not a production fix; structure is" width="820">

</div>

The cleanest cut: **169 of 239 atoms get a candidate answer under B at 4× budget; all 239 atoms get one under C at 1× budget.** The skill stack doesn't just lift answer quality — it makes convergence reliable on the atoms where bare-LLM stacks silently abandon.

Three takeaways from the experiment:

- **Retrieval is the small lever; skills + computation is the large one.** Domain-specific retrieval closes only a sliver of the gap on this benchmark. What moves the needle is structured tools that encode the conventions of the domain.
- **Generic harnesses + frontier LLMs stall on hard domain work.** Multi-step finance reasoning isn't a retrieval problem or a model-size problem — it's a *structure* problem. Without specialist tools, the LLM drifts in prose and never reaches a final answer.
- **Budget is not a substitute for structure.** Throwing more turns and wall-time at stalling agents is expensive, partial, and leaves the hardest questions unanswered. Structural cures scale; budget doesn't.

Full experiment, including the per-row failure-mode breakdown and the six retrieval adapters we authored against the Vals AI reference harness (forked at SHA `22a5ed49`): [coralbricks.ai/blog/coral-retrieval-vs-full-stack](https://coralbricks.ai/blog/coral-retrieval-vs-full-stack).

---

## What moved the headline numbers

<div align="center">

<img src="assets/alphacumen-issues-by-category.svg" alt="Fixes by category across all three benchmarks — convention encoding dominates, then multi-filing orchestration, data coverage, and retrieval ranking" width="820">

</div>

**1. Finance rules in code, not prose.** Inventory turnover, working capital, basis-point baselines — no single definition analysts agree on. Each rule lives in tested code (~a dozen dedicated computation tools), not in a natural-language prompt the model can paraphrase its way around. The other half is *time*: fiscal-year references resolve to each issuer's own calendar, not the default calendar year. The deeper finding: conventions don't even agree within a benchmark. FinanceBench wants *ending* inventory on one question and *average* on another — same dataset, same metric, opposite "correct" answers.

**2. Multi-filing orchestration.** *"Did the company beat the forecast it gave investors?"* — pull the forecast from one filing and the actual results from a later one, compare line by line. The planner routes this to the specialist that owns the filing-pair tool — one call returns both filings together, so the model can't mismatch quarters. The specialist renders the comparison into a structured form with a row for every line the company originally guided on, so the model can't skip non-headline items (stock-based comp, capex, share count) that often matter more than the headline.

**3. Data coverage.** Expanded ingestion to include proxy statements (board votes, exec comp), prospectuses (new-share details), registration statements (capital raises), foreign-private-issuer monthly revenue reports, and 10-K/A amendments — filing types typical finance datasets under-index. **Per-question asof** was the single biggest lever — making the asof horizon a per-question setting (not a global clamp) let the planner pull post-asof filings when the question explicitly references later quarters.

**4. Retrieval ranking.** Pure keyword search ranks single-cell table answers low because the surrounding narrative repeats the metric name dozens of times. Embeddings don't save you either: a full sentence in the narrative is a closer semantic match to the query than a bare label-and-number. The fix: route the query to the section of the filing that actually carries the granular numbers (disclosure notes, not management discussion), then run a structured-table extractor that records each cell as a *(metric, period, value)* triple — so the model asks for "gross margin in Q3 2024" and gets exactly that cell.

---

## Quick start

```bash
git clone https://github.com/Coral-Bricks-AI/coral-ai.git
cd coral-ai
export OPENAI_API_KEY=sk-...
python alphacumen-finance-benchmarks/examples/ask_alphacumen.py
```

Out of the box the kernel retrieval verbs are stubbed — the first call raises `NotImplementedError` with a redirect message. That's the demo, not a bug. Two paths to a real answer:

### Path A — Hosted (reproduce the benchmark numbers)

Coral Bricks runs AlphaCumen over a **~4.5 TB pre-processed finance corpus** — SEC filings, equity bars, news, knowledge graph — via the hosted runtime.

→ **[coralbricks.ai/alphacumen](https://coralbricks.ai/alphacumen)**

### Path B — Bring your own data

Read the code top-down — `swarm.py` → a specialist `persona_file` → `skills/<slug>/` — and replace the kernel-verb stubs in [`harness/stubs/`](../harness/stubs) with calls against your own backend (OpenSearch / Pinecone / DuckDB / your graph DB / your Python sandbox). The framework primitives and the finance conventions transfer; only the data plane is yours.

---

## What's in here

| File | What it does |
|---|---|
| [`swarm.py`](swarm.py) | The orchestrator — planner LLM call + parallel specialist fan-out per round, accumulates the common thread, calls the postprocessor on convergence |
| [`tools.py`](tools.py) | Finance-specific tool wrappers (BM25 SEC, equity bars, `compute_technicals`, `get_full_text`, …). Calls into the kernel-verb stubs by default. |
| [`roster.py`](roster.py) | `SpecialistConfig` per finance role + persona prompt loader |
| [`postprocessor.py`](postprocessor.py) | Terminal synthesis call — reads the converged common thread, writes the structured `final_answer` |
| [`memo.py`](memo.py) | Memo persistence (stubbed in OSS; no cross-call memory) |
| [`prompts/`](prompts/) | Persona system prompts + planner seed + postprocessor template |
| [`skills/`](skills/) | Finance skills — `compute_*` for math-bound conventions, `extract_*` for retrieval-shaped extraction |
| [`planner_skills/`](planner_skills/) | Cross-specialist routing playbooks (fiscal-period resolution, vocabulary mapping, dispatch routing) |
| [`planner/`](planner/) | Planner-side prompt variants |
| `capabilities.py`, `skill_registry.py`, `_langfuse.py`, `index_map.py` | Registry + observability glue |

### Depends on `harness/`

`alphacumen` imports from [`harness/`](../harness) for the ReAct loop, skill primitives, constraints, and the LLM client. They ship as one wheel (`cb-ia`) for now; nothing prevents `harness/` from being installed alone if you're building a non-finance harness.

### Benchmark queries + runnable example

The benchmark queries (Vals AI Finance Agent v2, FinanceBench) and the in-process example runner live in [`alphacumen-finance-benchmarks/`](../alphacumen-finance-benchmarks). Start with [`examples/ask_alphacumen.py`](../alphacumen-finance-benchmarks/examples/ask_alphacumen.py).

---

## Read more

- [State-of-the-art on Public Financial Benchmarks at $0.13 per Question](https://coralbricks.ai/blog/finance-benchmarks) — the benchmark methodology, every miss reviewed atom-by-atom, the cost math
- [Just the Retrieval Tools? Isolating What Closes the Gap on Vals AI Finance Agent v2](https://coralbricks.ai/blog/coral-retrieval-vs-full-stack) — three-stack A/B/C experiment, first-sweep failures, the budget-bump math

---

## Star, fork, contribute

If AlphaCumen is useful to you — or if you just want to follow along — **star the repo**. It's the single best signal we get that this work is worth doubling down on.

[![Star History Chart](https://api.star-history.com/svg?repos=Coral-Bricks-AI/coral-ai&type=Date)](https://star-history.com/#Coral-Bricks-AI/coral-ai&Date)

Issues and PRs welcome. We're particularly interested in:
- New domain instances (legal, medical, scientific) that adapt the swarm pattern outside finance
- Additional kernel-verb backends in [`harness/stubs/`](../harness/stubs)
- New finance skills under [`skills/`](skills/) — especially convention-encoded `compute_*` tools

---

## License

Apache 2.0 — see [LICENSE](../LICENSE) at the repo root.

## Authors

Hitesh Jain & Divy Vasal — [Coral Bricks](https://coralbricks.ai)
