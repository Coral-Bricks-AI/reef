# harness

Generic agent-harness primitives: a ReAct loop, a skill loader, run-level constraints with declarative tool-side enforcement, and a direct-provider LLM client.

Domain-agnostic core. The finance-specific layer that composes on top lives in [`alphacumen/`](../alphacumen).

## What's in here

- `react.py` — the ReAct loop (`run_react`), `chat_with_retry`, per-model watchdog + provider fallback, `Trajectory`/`Step` recording, tool-error-as-message serialization
- `llm.py` — direct-provider chat client. Dispatches by model prefix (`openai/...`, `anthropic/...`, `aws/...`, plus OpenAI-compatible proxies like `lilac/...`, `together/...`, `openrouter/...`, `cerebras/...`, `deepinfra/...`)
- `skill_fn.py` — the `@skill_fn` decorator that registers a Python callable against a skill id with a JSON Schema
- `skills_loader.py` — folder loader: `<slug>.md` (prose-only) and `<slug>/SKILL.md` + `impl.py` (folder-shaped with bound Python)
- `skill_tools.py` — model-facing dispatch tools (`invoke_skill_fn`, `load_skills`)
- `constraints.py` — `HarnessConstraints` dataclass (asof / tool_budget / max_rounds / allowed_indices / token_budget)
- `decorators.py` — the `@time_bounded` declarative tool contract
- `enforcement.py` — `LocalEnforcer` reads declarations, runs `before_tool_call` / `after_tool_call` around every dispatch
- `context.py` — per-run context propagation (ContextVar-based)
- `tool.py` — the `Tool` dataclass + OpenAI tool-schema serialization
- `stubs/` — stubs for the kernel retrieval verbs (BM25 / ANN / SQL / multihop / get / py) and the Python executor. The hosted Coral Bricks runtime replaces these with real backends over the prefab finance corpus; the open-source build raises `NotImplementedError` with a redirect message so framework code runs without a corpus

## Read the worked example

[`alphacumen/`](../alphacumen) is the finance instance of this harness — every primitive instantiated end-to-end. Inspect it as the reference design before building your own domain.

## License

Apache 2.0 — see [LICENSE](../LICENSE) at the repo root.

## Read more

Blog post: [Write Your Own Agent Harness](https://coralbricks.ai/blog/write-your-own-harness)
