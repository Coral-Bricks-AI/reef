# coral-ai

High-throughput inference for your agents — run many of them in parallel over your own private data, so you pay for your context once, not on every turn. Token economics and the swarm layer behind `alphacumen-finance-benchmarks`.

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](#)

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
| [`alphacumen-finance-benchmarks/`](alphacumen-finance-benchmarks/) | The AlphaCumen swarm layer — many finance specialists running in parallel over a private corpus; the pattern behind [our benchmark results](https://coralbricks.ai/blog/alphacumen-finance-benchmarks). |

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
├── alphacumen-finance-benchmarks/  # AlphaCumen swarm layer (finance specialists, in parallel)
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
