# coral-ai

High-throughput inference for your agents — run many of them in parallel over your own private data, so you pay for your context once, not on every turn. Token economics, the agent-harness framework, and the swarm layer behind AlphaCumen.

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](#)

> ⭐ **NEW — [`reef/`](reef/) + [`alphacumen/`](alphacumen/):** **Reef**, the
> open agent-harness framework behind our finance-benchmark results — and the
> worked finance instance built on top. **82.6%** on Vals AI Finance Agent v2
> with Kimi K2.6 (vs. 44.87% on the reference harness, same model). Run the
> framework hello-world: `python reef/examples/cocktails/ask.py`. Run the
> finance instance at scale on the Coral hosted runtime →
> **[coralbricks.ai/alphacumen](https://coralbricks.ai/alphacumen)** ·
> **[framework write-up](https://coralbricks.ai/blog/write-your-own-harness)**.

> ⭐ **Featured — [`claude-code-token-xray`](claude-code-token-xray/):** I broke a
> month of my own Claude Code logs into tokens, time, and cost. The surprise — you
> don't pay to generate, you pay to **re-read**: ~29M unique tokens get billed as
> **4.35B (~150×)**, and **84% of the bill is input**. Runs on your own `~/.claude`
> logs; nothing leaves your machine → **[the breakdown](claude-code-token-xray/)** ·
> **[full write-up](https://coralbricks.ai/blog/claude-code-token-xray)**.

## What's in here

Each subdirectory is an independently-installable package or example. They share a `coralbricks.*` PEP 420 namespace but have no hard runtime coupling — pick the pieces you need.

### Start here

| Path | What it is |
|---|---|
| [`claude-code-token-xray/`](claude-code-token-xray/) | Where your Claude Code tokens, time, and cost actually go — you pay to re-read, not generate (~29M unique tokens billed as 4.35B, ~150×). The problem this repo exists to address. Reads `~/.claude` only; nothing leaves your machine. |
| [`reef/`](reef/) | **Reef** — the open agent-harness framework. ReAct loop, skill primitives, run-level constraints, direct-provider LLM client. Domain-agnostic. The [framework write-up](https://coralbricks.ai/blog/write-your-own-harness) walks the primitives; [`reef/examples/cocktails/`](reef/examples/cocktails/) is the hello-world. |
| [`alphacumen/`](alphacumen/) | The worked finance instance of Reef — 7 agents, 69 skills, the postprocessor synthesis path. Examples + benchmark queries inside. The pattern behind [our finance-benchmark results](https://coralbricks.ai/blog/finance-benchmarks). |

### Building blocks

| Package | PyPI | What it is |
|---|---|---|
| [`context_prep/`](context_prep/) | [`coralbricks-context-prep`](https://pypi.org/project/coralbricks-context-prep/) | Build-time context prep: `clean → chunk → embed → enrich → hydrate`. Plain functions over `list[dict]` records — no loaders, no orchestrator. |
| [`integrations/airbyte/`](integrations/airbyte/) | [`coralbricks-airbyte`](https://pypi.org/project/coralbricks-airbyte/) | Ingestion bridge: reads Airbyte destination output (600+ connectors) into `list[dict]` records that feed `context_prep`. |
| [`py-gpu-inference/`](py-gpu-inference/) | [`coralbricks-gpu-inference`](https://pypi.org/project/coralbricks-gpu-inference/) | Production gRPC GPU embedding server. Token-bucket batching, dual backpressure, `torch.compile` + CUDA graphs — pure Python/PyTorch, no ONNX/TensorRT. |

### Framework integrations

| Package | PyPI | What it is |
|---|---|---|
| [`integrations/crewai/`](integrations/crewai/) | [`coralbricks-crewai`](https://pypi.org/project/coralbricks-crewai/) | CrewAI memory backend — `CoralBricksMemory` + `SearchCoralBricksMemoryTool`. |
| [`integrations/langchain/`](integrations/langchain/) | [`coralbricks-langchain`](https://pypi.org/project/coralbricks-langchain/) | LangChain memory backend — `CoralBricksMemory`, `CoralBricksRetriever`, agent tools (`store` / `search` / `forget`). |
| [`integrations/openclaw/`](integrations/openclaw/skills/persistent-agent-memory/) | — | OpenClaw skill `persistent-agent-memory`: bash-based `coral_store` / `coral_retrieve` / `coral_delete_matching`. |

### More examples

| Path | What it shows |
|---|---|
| [`event_scout/`](event_scout/) | A small agent that scrapes upcoming AI/tech events (Luma + Eventbrite) via TinyFish and dedups against CoralBricks memory across runs. |

## Repository layout

```
coral-ai/
├── claude-code-token-xray/  # where your Claude Code tokens, time, and cost go
├── reef/                   # Reef — agent-harness framework (ReAct, skills, constraints)
├── alphacumen/             # worked finance instance of Reef (7 agents, 69 skills)
├── context_prep/            # build-time context prep    → coralbricks.context_prep
├── py-gpu-inference/        # gRPC embedding server      → coralbricks.gpu_inference
├── integrations/
│   ├── airbyte/            # coralbricks-airbyte        → feeds context_prep
│   ├── crewai/              # coralbricks-crewai         → coralbricks_crewai
│   ├── langchain/           # coralbricks-langchain      → coralbricks_langchain
│   └── openclaw/            # persistent-agent-memory skill (bash)
└── event_scout/             # example: scraping agent + memory dedup
```

Each package owns its own `pyproject.toml`, `README.md`, and tests. Install only what you need.

## License

Apache 2.0 — see [LICENSE](LICENSE) for details.
