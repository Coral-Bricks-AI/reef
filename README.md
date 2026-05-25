# coral-ai

The memory layer for agentic AI. GPU-native embedding inference, build-time context preparation, and drop-in memory bindings for the agent frameworks people actually use.

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

### Core libraries

| Package | PyPI | What it is |
|---|---|---|
| [`py-gpu-inference/`](py-gpu-inference/) | [`coralbricks-gpu-inference`](https://pypi.org/project/coralbricks-gpu-inference/) | Production gRPC GPU embedding server. Token-bucket batching, dual backpressure, `torch.compile` + CUDA graphs — pure Python/PyTorch, no ONNX/TensorRT. |
| [`context_prep/`](context_prep/) | [`coralbricks-context-prep`](https://pypi.org/project/coralbricks-context-prep/) | Build-time context prep: `clean → chunk → embed → enrich → hydrate`. Plain functions over `list[dict]` records — no loaders, no orchestrator. |

### Framework integrations

| Package | PyPI | What it is |
|---|---|---|
| [`integrations/crewai/`](integrations/crewai/) | [`coralbricks-crewai`](https://pypi.org/project/coralbricks-crewai/) | CrewAI memory backend — `CoralBricksMemory` + `SearchCoralBricksMemoryTool`. |
| [`integrations/langchain/`](integrations/langchain/) | [`coralbricks-langchain`](https://pypi.org/project/coralbricks-langchain/) | LangChain memory backend — `CoralBricksMemory`, `CoralBricksRetriever`, agent tools (`store` / `search` / `forget`). |
| [`integrations/openclaw/`](integrations/openclaw/skills/persistent-agent-memory/) | — | OpenClaw skill `persistent-agent-memory`: bash-based `coral_store` / `coral_retrieve` / `coral_delete_matching`. |

### Examples

| Path | What it shows |
|---|---|
| [`event_scout/`](event_scout/) | A small agent that scrapes upcoming AI/tech events (Luma + Eventbrite) via TinyFish and dedups against CoralBricks memory across runs. |
| [`context_prep/examples/`](context_prep/examples/) | End-to-end RAG quickstart, knowledge-graph extraction, distributed `hydrate + merge`, and a fully-embedded RAG demo with DuckDB (`vss` + `duckpgq`) — vectors and graph in one local session, no servers. |
| [`claude-code-token-xray/`](claude-code-token-xray/) | Standalone scripts that break a month of your own local Claude Code logs into where the tokens, time, and cost actually go — the finding: you pay to re-read, not generate (~29M unique tokens billed as 4.35B, ~150×). Reads `~/.claude` only; nothing leaves your machine. |

## Quick start

### Run a GPU embedding server

```bash
pip install coralbricks-gpu-inference
python -m coralbricks.gpu_inference.grpc_server
```

`MODEL_PATH` accepts a local path, HuggingFace repo id (default: `answerdotai/ModernBERT-base`), or `s3://` URI. Full env-var reference and architecture notes in [`py-gpu-inference/README.md`](py-gpu-inference/README.md).

### Prepare context for retrieval

```bash
pip install 'coralbricks-context-prep[chunkers,embed-st]'
```

```python
from coralbricks.context_prep import clean, chunk, embed, enrich, hydrate

records = [{"id": "doc-1", "text": "<html>...$AAPL is up...</html>"}]
cleaned  = clean(records)
chunks   = chunk(cleaned,   strategy="sliding_token", target_tokens=512)
vectors  = embed(chunks,    model="st:BAAI/bge-m3")
enriched = enrich(cleaned,  extractors=["tickers", "dates", "urls"])
graph    = hydrate(enriched, graph="news")
```

Verbs, recipes, and the embedded-RAG tutorial live in [`context_prep/README.md`](context_prep/README.md).

### Give an agent persistent memory

```bash
pip install coralbricks-crewai      # or coralbricks-langchain
```

```python
from coralbricks_crewai import CoralBricksMemory, SearchCoralBricksMemoryTool

memory = CoralBricksMemory(api_key="...")
memory.get_or_create_memory_store("crewai:my-app")
memory.set_session_id("user-123")

memory.save_memory("Team prefers staying near Shibuya station.")
hits = memory.search_memory("hotel preferences", top_k=3)

tool = SearchCoralBricksMemoryTool(memory=memory)  # attach to a CrewAI Agent
```

LangChain has the same shape plus a `CoralBricksRetriever` for LCEL chains and a `get_tools(memory)` factory for agent loops. See each integration's README for the full API.

## Repository layout

```
coral-ai/
├── py-gpu-inference/        # gRPC embedding server      → coralbricks.gpu_inference
├── context_prep/            # build-time context prep    → coralbricks.context_prep
├── integrations/
│   ├── crewai/              # coralbricks-crewai         → coralbricks_crewai
│   ├── langchain/           # coralbricks-langchain      → coralbricks_langchain
│   └── openclaw/            # persistent-agent-memory skill (bash)
├── event_scout/             # example: scraping agent + memory dedup
└── claude-code-token-xray/  # example: token/time/cost breakdown of your Claude Code logs
```

Each package owns its own `pyproject.toml`, `README.md`, and tests. Install only what you need.

## Hosted vs. self-hosted

The integration packages (`crewai`, `langchain`, `openclaw`) talk to the hosted CoralBricks Memory API at `https://memory.coralbricks.ai` by default. Get an API key from the [CoralBricks web app](https://coralbricks.ai). To run end-to-end on your own hardware, point them at a self-hosted stack built around `py-gpu-inference` + `context_prep` + your vector store of choice.

## License

Apache 2.0 — see [LICENSE](LICENSE) for details.
