# Cocktails — the framework hello-world

The simplest end-to-end use of the harness: **one specialist, two skills, ~20 cocktails of data, ~50 lines of glue code.** No planner, no synthesizer, no `SpecialistConfig`. Just `run_react()` wired to a persona prompt and two skill-dispatch tools.

If you've read the [framework write-up](https://coralbricks.ai/blog/write-your-own-harness), this is the worked code behind it.

## Run it

```bash
git clone https://github.com/Coral-Bricks-AI/coral-ai.git
cd coral-ai
pip install -e .
export OPENAI_API_KEY=sk-...

python harness/examples/cocktails/ask.py "What's in a Negroni and how strong is it?"
```

Sample queries:

```bash
python harness/examples/cocktails/ask.py "Find me a refreshing rum cocktail without mint"
python harness/examples/cocktails/ask.py "Which classic gin cocktails are stirred?"
python harness/examples/cocktails/ask.py "How strong is an Espresso Martini?"
```

Any provider the framework supports works: switch `model="openai/gpt-4o-mini"` in `ask.py` to `"anthropic/claude-sonnet-4-6"`, `"aws/anthropic.claude-3-5-sonnet"`, etc., and set the matching env var.

## What's in here

| File | What it is |
|---|---|
| [`data/cocktails.json`](data/cocktails.json) | The corpus — 20 well-known cocktails with ingredients, ABV, glassware, instructions, tags |
| [`skills/search_cocktails/`](skills/search_cocktails/) | A BM25 search skill over the corpus. `SKILL.md` is the procedural playbook the model reads; `impl.py` is the `@skill_fn`-decorated Python callable. |
| [`skills/compute_alcohol_content/`](skills/compute_alcohol_content/) | A computation skill — volume-weighted ABV across a cocktail's ingredients. Same `SKILL.md` + `impl.py` shape. |
| [`bartender.md`](bartender.md) | The specialist's system prompt. Renders the skill index inline so the model knows what's loadable. |
| [`ask.py`](ask.py) | ~50-line runner. Calls `harness.react.run_react()` directly with the bartender persona + two dispatch tools. |

## What the framework gives you here

- **Skill primitive.** A `SKILL.md` + `impl.py` folder is the unit of reusable competence. The markdown is the routing playbook the model reads; the Python is the implementation the runtime dispatches to via `invoke_skill_fn(skill_id, fn, args)`. The `@skill_fn` decorator binds them together at import time.
- **Lazy loading.** The specialist sees a one-line *index* of skills in its system prompt and calls `load_skills(skill_ids=[…])` to pull bodies on demand. 70 skills indexed cost ~70 lines of context; only the loaded bodies pay tokens.
- **ReAct loop.** `harness.react.run_react()` is the loop: model → tool → model → tool → … until a no-tool answer. About 1,900 lines of Python with retry, watchdog, provider fallback, structured trajectory recording, and tool-error-as-message serialization.
- **Direct LLM client.** `harness.llm.chat()` talks OpenAI / Anthropic / Bedrock plus OpenAI-compatible proxies (Together, OpenRouter, Cerebras, DeepInfra, Lilac) via env-var auth.

## What this example does NOT use

Deliberately. Once you scale past one specialist:

- **Planner / synthesizer / `swarm.run()`** — orchestrates multi-specialist runs, dispatches in parallel, prunes between rounds, writes the final structured envelope. See [`alphacumen/swarm.py`](../../../alphacumen/swarm.py).
- **`SpecialistConfig`** — wraps one specialist's persona + tool roster + per-call budget for the planner to dispatch to.
- **`HarnessConstraints`** — declarative run-level invariants (asof / tool budgets / index allowlist) the planner enforces across dispatches.

When you have one specialist, none of that buys you anything. When you have six specialists arguing over which one knows the answer, all of it does.

## Where to go next

- [The framework write-up](https://coralbricks.ai/blog/write-your-own-harness) — the design patterns walked one section per primitive
- [`harness/`](../..) — the framework itself; read [`react.py`](../../react.py) and [`skill_fn.py`](../../skill_fn.py) to see how this hello-world hangs together
- [`alphacumen/`](../../../alphacumen) — the worked finance instance: 7 specialists, 69 skills, the planner + synthesizer scaffolding. Same primitives at a much larger scale.
