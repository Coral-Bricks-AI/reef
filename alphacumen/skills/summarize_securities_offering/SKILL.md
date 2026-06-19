---
id: summarize_securities_offering
when: Question asks about "key terms" / "size of offering" / "settlement" / "dividend rights" of a registered securities offering (424B5 prospectus supplement, 424B4 IPO, S-1 registration statement).
applies_to: [sector_analyst]
source_lines: 30, 513-558
---

**Dedicated tool: `summarize_securities_offering`.**

The prospectus describes the OFFERED deal (base shares +
over-allotment OPTION); the closing 8-K filed 1-3 days later
confirms the EXECUTED deal (whether the over-allotment was
exercised, final pricing, upsizes). The canonical answer is the
executed totals, not the offered base.

## Workflow

1. `bm25_sec` for the prospectus (`form_type:424B5` / `424B4` /
   `S-1`) with a tight `filed_at` window. `get_full_text` to
   extract all terms (size, settlement, price, dividend, etc.).
2. **Also `extract_filing_tables(ref="sec:<closing-8-K-acc>",
   item="2-2", table_keyword=<metric>)`** for the closing 8-K. The
   exhibits walker (see `ex99_exhibits_walker`) reaches the
   underwriting agreement + certificate of designations even when
   the 8-K isn't in OpenSearch. Discover the closing 8-K accession
   via `bm25_sec` with `ticker:<X> form_type:8-K filed_at_gte:
   <prospectus_date+1> filed_at_lte:<prospectus_date+5>`.
3. Call:
   ```
   summarize_securities_offering(
       ticker, security_name,
       base_shares, over_allotment_shares, over_allotment_exercised,
       price_per_share, closing_date, dividend_rate_pct, ...,
       purpose_text=<verbatim from prospectus>,
   )
   ```
   Pass `over_allotment_exercised=True` when the closing 8-K
   confirms the option was exercised. Pass the issuer's verbatim
   use-of-proceeds statement in `purpose_text` (include specific
   buckets like "core private equity portfolio companies", not just
   "general corporate purposes").
4. Quote `answer.formatted_atoms` as a bullet list in your
   `answer_summary` — one atom per bullet, in order.

**Do NOT add "Confidence: LOW" caveats** when news/8-K-metadata
searches don't corroborate a 424B5 — that's expected (424B5 is a
prospectus supplement, not a periodic report). Hedging caveats wreck
the contradiction atom.

## Common failure modes

- ❌ Quoting the 424B5 base-shares number when the over-allotment
  was exercised — pass `over_allotment_exercised=True` to the tool
  so it reports the executed total.
- ❌ Passing only "general corporate purposes" as `purpose_text`
  when the prospectus names specific use buckets.
- ❌ Writing the bullet list manually instead of using the tool's
  `formatted_atoms` output — paraphrasing costs atoms.
