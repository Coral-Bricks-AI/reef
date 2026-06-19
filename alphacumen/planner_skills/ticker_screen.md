---
id: ticker_screen
when: Query asks to FIND / IDENTIFY / SCREEN / SURFACE / PICK / LIST tickers ("find me", "which stocks", "screen for", "next NVIDIA", "short-squeeze candidates", "insider-buying").
applies_to: [stock_analyst, risk_analyst, sector_analyst]
source_lines: 212-234
---

- **Ticker-screen / candidate-list queries — MANDATORY stock_analyst
  dispatch with screening tools.** When the user asks to FIND /
  IDENTIFY / SCREEN / SURFACE / PICK / LIST tickers (any phrasing
  including "find me", "list of", "which stocks", "pick X
  candidates", "screen for", "show me high-volatility micro-caps",
  "next NVIDIA", "100-baggers", "high-momentum names", "short-squeeze
  candidates", "insider-buying signals"), your round-1 dispatch
  **must** include `stock_analyst` with `max_steps: 8` and an
  instruction that names at least one of `compute_market_cap`,
  `compute_float`, `fetch_insider_trades`, `compute_technicals`, or
  `compute_options_stats` as a required tool call. Pair with
  `risk_analyst` (for macro/event tail-risk context) and
  `sector_analyst` (for SEC-filing catalysts). **Converging in
  round 1 with zero tool calls AND `answerable: false` is a
  defect** — if specialists return empty, force one re-dispatch
  round naming a starter ticker universe (e.g. "scan recent SEC
  filings in the last 30 days for issuers with market-cap under
  $500M and recent 8-K catalysts; for each, call
  `fetch_insider_trades` and `compute_technicals` to populate the
  screen"). Only after that second round may you converge with
  `answerable: false`, and even then prefer `answerable: true` with
  a partial candidate list + data-gap disclosure (per the
  reframe-on-refusal rule below).
