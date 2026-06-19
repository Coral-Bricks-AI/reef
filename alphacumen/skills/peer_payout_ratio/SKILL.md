---
id: peer_payout_ratio
when: Question asks to compare a metric (most commonly dividend payout ratio) across an issuer and its peers, and rank highest-to-lowest. Most common for Consumer Staples and bank issuers.
applies_to: [sector_analyst]
source_lines: 31, 611-714
---

**Dedicated tool: `compute_payout_ratio` (one per ticker) OR
`compute_payout_ratio_peers` (Consumer Staples one-shot).**

Format each per-ticker output as `"<TICKER>: <value:.2f>"`.

## Peer-set selection

Use **S&P GICS sector-level peers**, not the narrowest
direct-competitor list. The biggest failure mode is picking a 3-4
pure-play direct-competitor list when the canonical cohort is 8+
tickers spanning the broader sector — the omitted tickers become
missing data points.

For a consumer-brand issuer specifically, the GICS Consumer Staples
sector spans these sub-industries that all count as peers for a
payout-ratio comparison:
- Beverages (soft drinks + brewers/wineries)
- Food Products (packaged food + meat producers)
- Household & Personal Products (cleaning, paper, personal care)

**Weighting matters.** For a Beverages issuer, analyst convention
clusters peers as **beverage + food** (the issuer is treated as a
packaged consumer-goods company), with household products as a
tertiary inclusion at most. The canonical distribution is ~2-4 Food
Products peers and 0-1 Household peers. Symmetric for Food Products
issuers (weighted toward beverage + other food) and Household
Products issuers (weighted toward HPP + adjacent personal-care;
food and beverage as tertiary).

A pure-play same-sub-industry list (e.g. only "soft-drink
competitors") will miss the food peers the convention includes.
Always pull from at least 2 of the 3 staples sub-industries using
the above weighting.

For tech, peers are GICS Information Technology / Communication
Services sub-industry members; for banks, the regional Federal
Reserve peer panel. When uncertain, ERR ON THE SIDE OF A WIDER PEER
SET (8-10 tickers) — extras don't hurt and you're less likely to
miss a canonical peer.

## Workflow (Consumer Staples issuers)

1. If the named issuer is in Consumer Staples (any of Beverages,
   Food Products, or Household & Personal Products), call
   `compute_payout_ratio_peers(issuer=<TICKER>, fy=<YYYY>)` **once**.
   The tool internally fans out across the standard S&P GICS
   Consumer Staples reference cohort (~18 large-caps spanning all
   three sub-industries) and returns:
   - `peers`: per-ticker dict with `payout_ratio` + `formatted_atom`
   - `ranked_descending`: ticker list sorted highest-to-lowest
   - `answer_summary_block`: pre-formatted bullet list ready to
     drop verbatim into `answer_summary`
2. Quote `answer_summary_block` directly in your `answer_summary`.
   Do NOT trim entries — even peers with 0.00 or no dividend stay
   in the list. Each per-ticker line is an independent atom; extras
   don't hurt, exclusions do.
3. Add a one-line ranking-summary statement after the block (e.g.
   "X ranks Nth of 18 with payout ratio Y.").

## Workflow (issuers OUTSIDE Consumer Staples, e.g. tech / banks / energy)

1. Identify a 6-10 ticker peer set using GICS sub-industry
   classification (Information Technology sub-industries for tech;
   Federal Reserve peer panel for banks; etc.).
2. Call `compute_payout_ratio(ticker=<X>, fy=<YYYY>)` once per peer
   ticker. Tool is cheap (~1 call per ticker).
3. If a peer returns an error (no XBRL DPS tag, non-dividend
   payer), include them in `answer_summary` as `"<TICKER>: <error
   / 0.00 (no dividend)>"` — don't drop silently.
4. Sort by payout ratio descending, write `answer_summary` as a
   ranked bullet list quoting **each tool result's `formatted_atom`
   verbatim**:
   `- <TICKER1>: <RATIO1>`
   `- <TICKER2>: <RATIO2>`
   …
   Then a one-line ranking-summary statement. Include EVERY ticker
   for which you called the tool — do NOT narrow the final list to
   "direct competitors" (or whatever the issuer's narrow category
   is) at answer time. Subset filtering at write time is the #1
   failure mode for this question class.

## Common failure modes

- ❌ Computing payout manually via bm25_sec + run_python per ticker
  — blows the tool budget for N≥4 peers. Use the tool.
- ❌ Picking the wrong peer set (a narrow same-sub-industry
  pure-play list instead of the broader S&P-classification cohort)
  — adjacent-industry competitors are usually part of the
  canonical comparison. Be inclusive at tool-call time AND keep
  them in the final answer.
- ❌ **Narrowing the final answer to a sub-category** (e.g.
  "Dividend-Paying [Sub-Industry] Competitors Only") after calling
  the tool for a wider set. Every ticker the tool returned belongs
  in the answer — extras don't hurt, exclusions do.
- ❌ Reporting payout as a percentage (`78.9%`, `85%`) instead of
  the decimal form (`0.79`, `0.85`). The tool returns each peer
  pre-formatted as `"<TICKER>: 0.XX"`; **quote that string
  verbatim** — do NOT convert to a percent or reformat as a table
  cell. The canonical answer expects the exact `"<TICKER>:
  <decimal>"` form.
- ❌ Dropping non-dividend payers silently. If a peer paid no
  dividend, include them as `"<TICKER>: 0.00 (no dividend)"` in
  the answer.
