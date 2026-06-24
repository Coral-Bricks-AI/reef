---
id: search_companies
when: Find companies matching a query — by ticker, name, sector, or any free-text descriptor (e.g. "AI chips", "money-center bank", "streaming"). Use this FIRST when the user names a company or describes a sector.
applies_to: [equity_analyst]
---

**Dedicated tool: `search_companies`. Call as your FIRST step when the user mentions a company or describes a sector.**

```
search_companies(
    query=<free text>,
    k=<int, default 5>,
)
```

Query examples:
- `"NVDA"` — ticker match
- `"Apple"` — exact name match
- `"semiconductor"` — sector match
- `"streaming"` — descriptor match
- `"money-center bank"` — multi-word sector match

Returns a ranked list of `{"ticker", "name", "sector", "score"}`.

After search, if the user asks anything quantitative (price return, performance,
how the stock did), follow up with `invoke_skill_fn(skill_id="compute_total_return",
fn="compute_total_return", args={"ticker": "<TICKER>"})` using the top result's
ticker.

Quote results faithfully — don't paraphrase the sector label. If no companies
match, say so honestly rather than inventing one.
