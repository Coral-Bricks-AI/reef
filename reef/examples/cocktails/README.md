# Cocktails — the Reef hello-world

The simplest end-to-end use of Reef: **one specialist, two skills, ~20 cocktails of data, ~50 lines of glue.** No planner, no synthesizer, no `SpecialistConfig`. Just `run_react()` wired to a persona prompt and two skill-dispatch tools.

If you've read the [Reef write-up](https://coralbricks.ai/blog/write-your-own-harness), this is the worked code behind it.

## Run it

```bash
git clone https://github.com/Coral-Bricks-AI/coral-ai.git
cd coral-ai
pip install -e .
export LLM_API_KEY=sk-...

python reef/examples/cocktails/ask.py "What's in a Negroni and how strong is it?"
```

Sample queries:

```bash
python reef/examples/cocktails/ask.py "Find me a refreshing rum cocktail without mint"
python reef/examples/cocktails/ask.py "Which classic gin cocktails are stirred?"
python reef/examples/cocktails/ask.py "How strong is an Espresso Martini?"
```

Any provider Reef supports works: switch `model="openai/gpt-4o-mini"` in `ask.py` to `"anthropic/claude-sonnet-4-6"`, `"aws/anthropic.claude-3-5-sonnet"`, etc., and set the matching env var.

## What's on disk

```
examples/cocktails/
├── ask.py                     # 50-line runner — calls run_react()
├── bartender.md               # the system prompt (with {skill_index} placeholder)
├── data/cocktails.json        # the corpus (20 cocktails)
└── skills/
    ├── search_cocktails/
    │   ├── SKILL.md           # routing playbook the model reads
    │   └── impl.py            # @skill_fn-decorated Python the runtime calls
    └── compute_alcohol_content/
        ├── SKILL.md
        └── impl.py
```

| File | Role |
|---|---|
| [`data/cocktails.json`](data/cocktails.json) | The corpus — 20 well-known cocktails with ingredients, ABV, glassware, instructions, tags |
| [`skills/search_cocktails/`](skills/search_cocktails/) | BM25 search over the corpus |
| [`skills/compute_alcohol_content/`](skills/compute_alcohol_content/) | Volume-weighted ABV across a cocktail's ingredients |
| [`bartender.md`](bartender.md) | The specialist's system prompt — renders the skill index inline |
| [`ask.py`](ask.py) | ~50-line runner. Calls `reef.react.run_react()` directly with the bartender persona + two dispatch tools |

## One skill, end to end

Two files, sharing a slug. Markdown for the model, Python for the runtime.

[`skills/search_cocktails/SKILL.md`](skills/search_cocktails/SKILL.md):

```markdown
---
id: search_cocktails
when: Find cocktails by name, ingredient, style, or tag. Use FIRST when the user
      names a cocktail or describes a style.
applies_to: [bartender]
---

Call `search_cocktails(query=<free text>, k=<int, default 5>)`.

Returns a ranked list of `{"id", "name", "tags", "ingredient_names"}`.
After search, if the question is quantitative, follow up with
`compute_alcohol_content` using the top result's `id`.
```

[`skills/search_cocktails/impl.py`](skills/search_cocktails/impl.py):

```python
from reef.skill_fn import skill_fn

@skill_fn(
    skill_id="search_cocktails",
    description="Rank cocktails by BM25 over name + tags + ingredient names.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "k": {"type": "integer", "default": 5},
        },
        "required": ["query"],
    },
)
def search_cocktails(*, query: str, k: int = 5):
    ...  # BM25 over the corpus
    return {"query": query, "results": results}
```

The decorator registers the callable in a process-global registry at import time. The model dispatches by id — `invoke_skill_fn(skill_id="search_cocktails", fn="search_cocktails", args={...})` — and the runtime runs your Python.

## Skills load lazily

The model sees only a one-line *index* of every skill in its system prompt:

```
- search_cocktails  — Find cocktails by name, ingredient, style, or tag. Use FIRST...
- compute_alcohol_content — Volume-weighted ABV across a cocktail's ingredients.
```

To use one, it calls `load_skill(skill_ids=["search_cocktails"])` and the body of `SKILL.md` plus the JSON Schema for `invoke_skill_fn` get spliced into the thread. Seventy skills indexed cost ~70 lines of context; only the loaded bodies pay tokens.

## What this example does NOT use

Deliberately. Once you scale past one specialist:

- **Planner / synthesizer / `swarm.run()`** — orchestrates multi-specialist runs, dispatches in parallel, prunes between rounds, writes the final structured envelope. See [`alphacumen/swarm.py`](../../../alphacumen/swarm.py).
- **`SpecialistConfig`** — wraps one specialist's persona + tool roster + per-call budget for the planner to dispatch to.
- **`HarnessConstraints`** — declarative run-level invariants (asof / tool budgets / index allowlist) the planner enforces across dispatches.

When you have one specialist, none of that buys you anything. When you have six specialists arguing over which one knows the answer, all of it does.

## Where to go next

- [The Reef write-up](https://coralbricks.ai/blog/write-your-own-harness) — design rationale walked one section per primitive
- [`reef/`](../..) — the framework itself; read [`react.py`](../../react.py) and [`skill_fn.py`](../../skill_fn.py) to see how this hello-world hangs together
- [`alphacumen/`](../../../alphacumen) — the worked finance instance: 7 specialists, 69 skills, the planner + synthesizer scaffolding. Same primitives at a much larger scale.
