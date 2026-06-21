# harness

**A real agent in 30 lines of Python.** No framework you have to memorize. No graph DSL. No `AgentExecutor` of mystery. Just a ReAct loop, skills as folders, and a multi-provider LLM client — the four primitives you need to ship an agent, and nothing else.

```python
from harness.react import run_react
from harness.skills_loader import load_skills, render_index, render_loaded
from harness.skill_tools import INVOKE_SKILL_FN, make_load_skills_tool

SKILLS = load_skills("./skills")  # discovers SKILL.md + impl.py folders
LOAD = make_load_skills_tool(
    lambda ids: render_loaded(list(ids), skills=SKILLS),
)

prompt = f"You are a bartender.\n\n## Skills\n{render_index(SKILLS)}"

traj = run_react(
    model="openai/gpt-4o-mini",
    system_prompt=prompt,
    user_message="What's in a Negroni and how strong is it?",
    tools=[LOAD, INVOKE_SKILL_FN],
)
print(traj.final_message["content"])
```

That's a working agent. It plans, calls tools, recovers from errors, and answers. Swap `openai/gpt-4o-mini` for `anthropic/claude-sonnet-4-6` or `aws/...` and it keeps working.

---

## Why a harness instead of a framework

A framework asks you to learn its abstractions (chains, graphs, runnables, executors, memories) before you can ship anything. A harness gives you the loop, the dispatch, and the contract — and stays out of the way.

- **No DSL.** Skills are markdown + Python. You read them. The model reads them. Git diffs them.
- **No magic context.** What the model sees is a string you can `print()`. The skill index is rendered inline at the top of the system prompt; bodies load on demand.
- **One loop, ~1,900 lines.** `run_react` does retry, watchdog timeouts, provider fallback, structured trajectory recording, and tool-error-as-message serialization. Read it in one sitting.
- **Provider-neutral.** OpenAI, Anthropic, Bedrock, plus OpenAI-compatible proxies (Together, OpenRouter, Cerebras, DeepInfra, Lilac). Dispatch is one prefix on the model string.
- **Apache 2.0.** Fork it, vendor it, rip pieces out.

---

## Install

```bash
git clone https://github.com/Coral-Bricks-AI/coral-ai.git
cd coral-ai
pip install -e .
export OPENAI_API_KEY=sk-...
```

Run the worked example:

```bash
python harness/examples/cocktails/ask.py "What's in a Negroni and how strong is it?"
```

```
Q: What's in a Negroni and how strong is it?

A: A Negroni is 30 ml gin, 30 ml sweet vermouth, and 30 ml Campari, stirred
   over ice and served in a rocks glass with an orange peel. It comes out
   to roughly 27% ABV — a strong, bitter aperitivo.
```

More sample queries:

```bash
python harness/examples/cocktails/ask.py "Find me a refreshing rum cocktail without mint"
python harness/examples/cocktails/ask.py "Which classic gin cocktails are stirred?"
python harness/examples/cocktails/ask.py "How strong is an Espresso Martini?"
```

---

## The cocktails example, in full

`examples/cocktails/` is the hello-world. One specialist (a bartender), two skills (BM25 search + ABV math), 20 cocktails of data, ~50 lines of glue. The whole thing fits on one screen.

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

### A skill is a folder

Two files. Markdown for the model, Python for the runtime. They share a slug.

`skills/search_cocktails/SKILL.md`:

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

`skills/search_cocktails/impl.py`:

```python
from harness.skill_fn import skill_fn

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

### Skills load lazily

The model sees only a one-line *index* of every skill in its system prompt:

```
- search_cocktails  — Find cocktails by name, ingredient, style, or tag. Use FIRST...
- compute_alcohol_content — Volume-weighted ABV across a cocktail's ingredients.
```

To use one, it calls `load_skills(skill_ids=["search_cocktails"])` and the body of `SKILL.md` plus the JSON Schema for `invoke_skill_fn` get spliced into the thread. Seventy skills indexed cost ~70 lines of context; only the loaded bodies pay tokens.

---

## Architecture

How the primitives wire together at runtime:

```mermaid
flowchart LR
    user([user_message]) --> loop
    skills[(skills/<br/>SKILL.md + impl.py)] -. index .-> prompt[system prompt<br/>+ skill index]
    prompt --> loop
    loop{{run_react}} <-->|chat| llm[llm.chat<br/>openai · anthropic · aws · ...]
    loop <-->|tool calls| tools[Tool dispatch<br/>load_skills · invoke_skill_fn]
    skills -. @skill_fn .-> tools
    loop --> answer([final_message])
```

Three loops of data flow: (1) the **skill index** is rendered into the system prompt once at startup, (2) the **ReAct loop** alternates between `llm.chat` and tool dispatch until the model emits an answer with no tool calls, and (3) each tool dispatch either pulls a skill body in (`load_skills`) or runs a registered `@skill_fn` callable (`invoke_skill_fn`). That's the whole picture — no graph, no agent class, no orchestrator.

---

## What's in this directory

| File | Role |
|---|---|
| `react.py` | The ReAct loop — `run_react`, `chat_with_retry`, per-model watchdog + provider fallback, `Trajectory`/`Step` recording |
| `llm.py` | Direct-provider chat client. Dispatch by model prefix (`openai/`, `anthropic/`, `aws/`, plus OpenAI-compatible proxies: `lilac/`, `together/`, `openrouter/`, `cerebras/`, `deepinfra/`) |
| `skill_fn.py` | The `@skill_fn` decorator — register a Python callable against a skill id and a JSON Schema |
| `skills_loader.py` | Folder loader. `<slug>.md` (prose-only) and `<slug>/SKILL.md` + `impl.py` (folder-shaped with bound Python) |
| `skill_tools.py` | Model-facing dispatch tools (`INVOKE_SKILL_FN`, `make_load_skills_tool`) |
| `tool.py` | The `Tool` dataclass + OpenAI tool-schema serialization |
| `constraints.py` | `HarnessConstraints` dataclass (asof / tool_budget / max_rounds / allowed_indices / token_budget) |
| `decorators.py` | The `@time_bounded` declarative tool contract |
| `enforcement.py` | `LocalEnforcer` reads declarations, runs `before_tool_call` / `after_tool_call` around every dispatch |
| `context.py` | Per-run context propagation (ContextVar-based) |
| `stubs/` | Stubs for retrieval verbs (BM25 / ANN / SQL / multihop / get / py) and the Python executor. Replace with your own backends. |

The harness directory has **zero alphacumen imports** — it's a standalone, domain-agnostic library. Anything finance-specific lives in [`alphacumen/`](../alphacumen).

---

## Scaling up

One specialist + two skills is the hello-world. The same primitives compose to many. [`alphacumen/`](../alphacumen) is a worked instance with seven specialists and sixty-nine skills running over a finance corpus, plus a planner that dispatches in parallel, prunes between rounds, and writes a final structured envelope. Read it as the reference design when one specialist isn't enough.

The things `alphacumen/` adds on top of the cocktails shape:

- **`SpecialistConfig`** — bundles a persona + tool roster + per-call budget for the planner to dispatch to.
- **Planner / synthesizer / `swarm.run()`** — orchestrates multi-specialist rounds.
- **`HarnessConstraints`** — declarative run-level invariants (asof / tool budgets / index allowlist) enforced across dispatches.

When you have one specialist, none of that buys you anything. When you have six specialists arguing over which one knows the answer, all of it does.

---

## FAQ

**Is this production ready?**
Yes. We built [AlphaCumen](../alphacumen) on this harness and it beat every public finance benchmark we ran (FinanceBench, ValsAI). The same `run_react` loop, the same `@skill_fn` dispatch, the same `llm.chat` provider client you see in the cocktails example are the ones that drove those runs — at seven specialists, sixty-nine skills, and tens of thousands of evaluations. If it can hold up there, it can hold up under your workload.

**How does this compare to LangChain / LangGraph / CrewAI / AutoGen?**
Those are frameworks: they own the loop, the abstractions, and the lifecycle. You learn their objects (chains, runnables, agents, crews, graphs) before you can ship anything. The harness is the opposite — it owns nothing you can't read in one sitting. The whole ReAct loop is one function. Skills are folders. Tools are dataclasses. If you want a framework's ergonomics, take a framework; if you want the dispatch contract and nothing else, take this.

**Why "skills" instead of "tools"?**
A tool is one callable. A skill is a unit of *reusable competence* — a markdown playbook the model reads (when to use it, what the I/O contract is, how to chain it) plus zero-or-more Python callables it can dispatch to. The split is what makes lazy loading work: 70 skills cost ~70 lines of context in the index; only the loaded bodies pay tokens.

**Can I use my own LLM provider?**
Yes. `llm.chat` dispatches by the model-string prefix: `openai/`, `anthropic/`, `aws/` (Bedrock), plus OpenAI-compatible proxies (`together/`, `openrouter/`, `cerebras/`, `deepinfra/`, `lilac/`). If your provider speaks the OpenAI chat-completions shape, add it in a dozen lines. If it doesn't, fork `_chat_anthropic` as a template.

**Can I bring my own retrieval backend?**
Yes. `harness/stubs/tools.py` is where the kernel verbs (`bm25`, `ann`, `sql`, `multihop`, `get`, `py`) live as stubs. Replace them with your own backend (OpenSearch, Pinecone, DuckDB, whatever) and the rest of the harness keeps working. Skills don't know what's behind the verb — they just call it.

**Does it persist sessions / memory across runs?**
Not in this build. The `Trajectory` is a per-run record; persistence is a layer you wire on top.

**What's the dependency footprint?**
The framework itself is `openai`, `anthropic`, optional `boto3` (Bedrock), and stdlib. No LangChain. No LangGraph. No vector DB. Skills can pull in whatever they want at the `impl.py` layer.

---

## Read more

- **Blog: [Write Your Own Agent Harness](https://coralbricks.ai/blog/write-your-own-harness)** — the design walked one section per primitive
- [`examples/cocktails/`](examples/cocktails) — the worked code behind this README
- [`alphacumen/`](../alphacumen) — the multi-specialist reference instance

## License

Apache 2.0 — see [LICENSE](../LICENSE) at the repo root.
