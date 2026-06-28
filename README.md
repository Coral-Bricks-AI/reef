# Reef

**If your agent improves when you add instructions, then suddenly gets worse, you may not have a prompting problem. You may have a harness problem.**

We saw this firsthand building a finance research harness steering Kimi K2.6. We were about to announce during the week of the Opus 4.8 launch, riding a wide lead on the then-leading benchmark, Vals AI v1. Then Vals AI released v2 and Anthropic showcased Opus 4.8 on it — moving the spotlight to the harder benchmark overnight. On v2, our edge had collapsed to zero. Prompts had grown past 2,500 lines of instructions and 50+ tool definitions, many overlapping or conflicting. Adding more instructions stopped moving the needle. That's when Reef was born.

What you'll find in this repo: the framework itself ([`reef/`](reef/)), the finance harness that came out the other side at **82.6%** on Vals AI Finance v2 and **$0.13 per query** ([`alphacumen/`](alphacumen/)), the autonomous optimization-loop coordinator that drove **+59pp on HotpotQA** across **108 unattended LoRA experiments** ([`polyp/`](polyp/)), and the token-accounting diagnostic that started everything ([`claude-code-token-xray/`](claude-code-token-xray/)) — all Apache 2.0.

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](#)
[![Stars](https://img.shields.io/github/stars/Coral-Bricks-AI/reef?style=social)](https://github.com/Coral-Bricks-AI/reef)

---

## What's in the project

Three independently-installable packages plus a diagnostic. Each has its own README, its own quickstart, and its own story.

| Path | What it is | Headline |
|---|---|---|
| [`reef/`](reef/) | **The harness framework.** ReAct loop, skills-as-folders, declarative runtime constraints, direct-provider LLM client. Domain-agnostic, ~1,900 LOC. | The substrate everything else builds on. |
| [`alphacumen/`](alphacumen/) | **Finance agent harness built on Reef.** 7 specialists, 69 skills, the postprocessor synthesis path. | **82.6%** on Vals AI Finance Agent v2 · **90%** on Vals AI v1.1 · **89.3%** on FinanceBench · **$0.13/query** |
| [`polyp/`](polyp/) | **Autonomous optimization-loop coordinator.** Postgres-backed state machine: Architect → Worker → Analyzer → Auto-suggester. Drives any try-evaluate-iterate problem; agent-driver-agnostic (Claude Code by default, your own Reef harness, or any coding agent). | **+59pp on HotpotQA** over 108 unattended LoRA fine-tuning experiments on gpt-oss-20b in 3 days. |
| [`claude-code-token-xray/`](claude-code-token-xray/) | **The diagnostic that started the company.** Breaks your `~/.claude` logs into tokens, time, and cost. | ~29M unique tokens billed as **4.35B (~150×)** — **84% of the bill is input**. Nothing leaves your machine. |

## Start here

**Want to build your own domain agent?** → [`reef/`](reef/) — read the [framework write-up](https://coralbricks.ai/blog/write-a-winning-agent-harness), copy [`reef/examples/equities/`](reef/examples/equities/), rewrite four pieces.

**Want state-of-the-art finance answers right now?** → [`alphacumen/`](alphacumen/) — `pip install`, set `CORAL_API_KEY`, ask. Runs against the hosted ~4.5 TB pre-processed finance corpus.

**Want to run unattended optimization sweeps?** → [`polyp/`](polyp/) — stand up Postgres, `cbq init-db`, point at a worker script. Read the [LoRA trajectory writeup](https://coralbricks.ai/research/lora-trajectory) for a real-world run.

**Curious where your Claude Code bill actually goes?** → [`claude-code-token-xray/`](claude-code-token-xray/) — runs on your own logs in a few seconds. [Full breakdown](https://coralbricks.ai/blog/claude-code-token-xray).

## How the pieces fit

```
        ┌─────────────────────────────────────────┐
        │              reef/  (framework)          │
        │   ReAct loop · skills · constraints      │
        │   provider-neutral LLM client            │
        └─────────────────────────────────────────┘
                  ▲                       ▲
                  │ imports               │ drives (one option)
                  │                       │
        ┌─────────┴──────────┐   ┌────────┴─────────┐
        │    alphacumen/     │   │      polyp/      │
        │ 7 specialists,     │   │  Architect →     │
        │ 69 finance skills  │   │  Worker →        │
        │ 82.6% Vals v2      │   │  Analyzer →      │
        │                    │   │  Auto-suggester  │
        └────────────────────┘   └──────────────────┘
```

- **AlphaCumen → Reef**: hard import. Same `run_react` loop, `@skill_fn` dispatch, `llm.chat` client.
- **Polyp → Reef**: optional. Polyp coordinates the loop; the agent driving each phase is yours — Claude Code by default, or wire a Reef harness if you want a domain-specialized Architect.
- **Reef → nothing**: zero finance, zero queue, zero opinion about your data plane. Vendor on its own.

## Repository layout

```
reef/                          # this repo
├── reef/                      # the framework (agent harness primitives)
├── alphacumen/                # worked finance instance
├── polyp/                     # autonomous optimization loop coordinator
└── claude-code-token-xray/    # the diagnostic that started it all
```

Each package owns its own `pyproject.toml`, `README.md`, and tests. Install only what you need.

## License

Apache 2.0 — see [LICENSE](LICENSE).

## Authors

Hitesh Jain & Divy Vasal — [Coral Bricks](https://coralbricks.ai)
