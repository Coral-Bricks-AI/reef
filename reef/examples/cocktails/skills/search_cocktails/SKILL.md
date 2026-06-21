---
id: search_cocktails
when: Find cocktails matching a query — by name, ingredient, style, tag, or any free-text term. Use this FIRST when the user names a cocktail or describes a style.
applies_to: [bartender]
---

**Dedicated tool: `search_cocktails`. Call as your FIRST step when the user mentions a cocktail or describes a style.**

```
search_cocktails(
    query=<free text>,
    k=<int, default 5>,
)
```

Query examples:
- `"negroni"` — exact name match
- `"gin citrus"` — style match across multiple terms
- `"italian aperitivo"` — tag match
- `"campari"` — ingredient match

Returns a ranked list of `{"id", "name", "tags", "ingredient_names"}`.

After search, if the user asks anything quantitative (alcohol content, ABV,
strength), follow up with `invoke_skill_fn(skill_id="compute_alcohol_content",
fn="compute_alcohol_content", args={"cocktail_id": "<id>"})` using the
top result's `id`.

Quote results faithfully — don't paraphrase ingredient lists. If no cocktails
match, say so honestly rather than inventing one.
