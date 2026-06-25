# Equities — the Reef hello-world

The simplest end-to-end use of Reef: **one specialist, two skills, ~20 well-known tickers of data, ~50 lines of glue.** No planner, no synthesizer, no `SpecialistConfig`. Just `run_react()` wired to a persona prompt and two skill-dispatch tools.

If you've read the [Reef write-up](https://coralbricks.ai/blog/write-a-winning-agent-harness), this is the worked code behind it. For the full-scale version of the same primitives, see [`alphacumen/`](../../../alphacumen) — 7 specialists, 69 skills, a planner, and runtime constraints.

> **Data is mock.** `data/companies.json` is ~20 well-known tickers with **fabricated point-in-time prices**. The numbers are internally consistent and plausible, but not real market data. Don't use this example to make decisions — it exists to show the Reef wiring.

## Run it

```bash
git clone https://github.com/Coral-Bricks-AI/coral-ai.git
cd coral-ai
pip install -e .
export LLM_API_KEY=sk-...

python reef/examples/equities/ask.py "How has NVDA performed over the last year?"
```

Sample queries:

```bash
python reef/examples/equities/ask.py "Which money-center banks are in the corpus?"
python reef/examples/equities/ask.py "What does Moderna do and how has the stock done?"
python reef/examples/equities/ask.py "Compare AMD and INTC over the last 12 months."
```

Any provider Reef supports works: pass `--model <provider>/<model>` (e.g., `--model anthropic/claude-sonnet-4-6`, `--model together/kimi-k2.6`, `--model aws/anthropic.claude-3-5-sonnet`) and set the matching env var (`ANTHROPIC_API_KEY`, `TOGETHER_API_KEY`, AWS creds, etc.).

## What's on disk

```
examples/equities/
├── ask.py                     # 50-line runner — calls run_react()
├── analyst.md                 # the system prompt (with {skill_index} placeholder)
├── data/companies.json        # the corpus (20 mock companies)
└── skills/
    ├── search_companies/
    │   ├── SKILL.md           # routing playbook the model reads
    │   └── impl.py            # @skill_fn-decorated Python the runtime calls
    └── compute_total_return/
        ├── SKILL.md
        └── impl.py
```

| File | Role |
|---|---|
| [`data/companies.json`](data/companies.json) | The corpus — 20 well-known tickers with name, sector, description, and mock 1y price points |
| [`skills/search_companies/`](skills/search_companies/) | BM25 search over ticker + name + sector + description |
| [`skills/compute_total_return/`](skills/compute_total_return/) | Trailing 1-year price return for a ticker |
| [`analyst.md`](analyst.md) | The specialist's system prompt — renders the skill index inline |
| [`ask.py`](ask.py) | ~50-line runner. Calls `reef.react.run_react()` directly with the analyst persona + two dispatch tools |

## One skill, end to end

Two files, sharing a slug. Markdown for the model, Python for the runtime.

[`skills/search_companies/SKILL.md`](skills/search_companies/SKILL.md):

```markdown
---
id: search_companies
when: Find companies by ticker, name, sector, or any free-text descriptor.
      Use FIRST when the user names a company or describes a sector.
applies_to: [equity_analyst]
---

Call `search_companies(query=<free text>, k=<int, default 5>)`.

Returns a ranked list of `{"ticker", "name", "sector", "score"}`.
After search, if the question is quantitative, follow up with
`compute_total_return` using the top result's `ticker`.
```

[`skills/search_companies/impl.py`](skills/search_companies/impl.py):

```python
from reef.skill_fn import skill_fn

@skill_fn(
    skill_id="search_companies",
    description="Rank companies by BM25 over ticker + name + sector + description.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "k": {"type": "integer", "default": 5},
        },
        "required": ["query"],
    },
)
def search_companies(*, query: str, k: int = 5):
    ...  # BM25 over the corpus
    return {"query": query, "results": results}
```

The decorator registers the callable in a process-global registry at import time. The model dispatches by id — `invoke_skill_fn(skill_id="search_companies", fn="search_companies", args={...})` — and the runtime runs your Python.

## Skills load lazily

The model sees only a one-line *index* of every skill in its system prompt:

```
- search_companies     — Find companies by ticker, name, sector, or descriptor. Use FIRST...
- compute_total_return — Trailing 1-year price return for a ticker.
```

To use one, it calls `load_skill(skill_ids=["search_companies"])` and the body of `SKILL.md` plus the JSON Schema for `invoke_skill_fn` get spliced into the thread. Seventy skills indexed cost ~70 lines of context; only the loaded bodies pay tokens.

## What this example does NOT use

Deliberately. Once you scale past one specialist:

- **Planner / synthesizer / `swarm.run()`** — orchestrates multi-specialist runs, dispatches in parallel, prunes between rounds, writes the final structured envelope. See [`alphacumen/swarm.py`](../../../alphacumen/swarm.py).
- **`SpecialistConfig`** — wraps one specialist's persona + tool roster + per-call budget for the planner to dispatch to.
- **`HarnessConstraints`** — declarative run-level invariants (asof / tool budgets / index allowlist) the planner enforces across dispatches.
- **Real retrieval** — production AlphaCumen pulls from EDGAR + a half-dozen indexed corpora; this example reads one in-memory JSON.

When you have one specialist over a 20-row corpus, none of that buys you anything. When you have six specialists arguing across thousands of filings, all of it does.

## Where to go next

- [The Reef write-up](https://coralbricks.ai/blog/write-a-winning-agent-harness) — design rationale walked one section per primitive
- [`reef/`](../..) — the framework itself; read [`react.py`](../../react.py) and [`skill_fn.py`](../../skill_fn.py) to see how this hello-world hangs together
- [`alphacumen/`](../../../alphacumen) — the worked finance instance: 7 specialists, 69 skills, the planner + synthesizer scaffolding. Same primitives at a much larger scale.
