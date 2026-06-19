---
id: event_listing
when: Open-ended event-listing for an issuer — "what material events have hit [X] since [date]" / "key developments for [X] over [period]" / "summarize [X]'s major filings since [date]". Answer is fundamentally a *list of dated headlines*.
applies_to: [sector_analyst]
source_lines: 32, 716-776
---

**Use `bm25_sec` for the *list*, NOT `get_full_text` on every body.**

## The trap to avoid

The model's default instinct is to call `bm25_sec(form_type:8-K,
filed_at_gte:<start>)` once, then `get_full_text` on each top hit to
"make sure" the summary is complete. For a 12-month "events since"
question this typically becomes 6-10 `get_full_text` calls of ~17KB
each — context grows from ~35K input tokens at round 1 to ~70K by
round 8, the model hits the `max_steps=12` ceiling without
converging, and the runtime has to coerce a final answer from a
truncated thread. In a recent production run this pattern alone cost
~50s of wallclock (12 rounds, ~600K input tokens for sector_analyst
alone). The 8-K's *Item code* + the bm25_sec snippet title is
already enough to write a one-line event summary in 95% of cases.

## Workflow

1. **Single `bm25_sec` call** with `form_type:"8-K"` +
   `event_date_gte`/`event_date_lte` covering the asked period +
   `k:20` (high enough to enumerate the period's material filings;
   recency-floor will sort by filed_at DESC).
2. Read **only the bm25 hit metadata** for each result — the
   `source.item_codes` array, the `source.event_date`, the
   `source.title` / first 200 chars of `source.body_snippet`. That
   gives you: date, Item type (2.02 earnings, 5.02 officer change,
   7.01 reg-FD disclosure, 8.01 other events, 1.01 material
   agreement, 5.07 vote results, 2.05 cost-action, etc.), and the
   headline.
3. Compose `answer_summary` as a chronological bullet list:
   `- YYYY-MM-DD — [Item N.NN] <one-line event description from
   snippet>`. Group adjacent same-Item entries if appropriate (e.g.
   "2025-Q1 through 2025-Q4 earnings: see Items 2.02 on
   Jan/Apr/Jul/Oct"). One Item-code lookup table is enough to
   describe the bulk of the year.
4. **Only call `get_full_text` if the snippet is genuinely
   ambiguous on a specific filing** (e.g. the bm25 title says
   "Press Release" with no further hint). Cap to ≤2
   `get_full_text` calls per query at most.
5. For 10-K/10-Q items the question explicitly asks for ("risk
   factor changes since FY[Y-1]"), pull those separately with a
   second `bm25_sec` call narrowed to the form.

## Common failure modes

- ❌ `get_full_text` on every 8-K hit "to be thorough." 6+
  full-text calls = ~100K input tokens of body text the model
  doesn't need to read. The Item code already tells you what the
  filing is about.
- ❌ Doing one `bm25_sec` per Item type (one for 2.02, one for
  5.02, one for 7.01, …). The default `bm25_sec` query with no
  `item_codes` filter returns the period's full event mix in one
  call.
- ❌ Hedging "I'd need to read every filing to be sure" in the
  final answer. The Item-code taxonomy + the snippet titles are the
  SEC's own categorization of what counts as a material event —
  quoting them directly IS the answer.
