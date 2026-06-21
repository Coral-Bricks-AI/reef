---
id: compute_alcohol_content
when: Compute ABV (alcohol by volume) of a cocktail by id — accounts for ingredient ABVs weighted by volume across the build.
applies_to: [bartender]
---

**Dedicated tool: `compute_alcohol_content`. Call AFTER `search_cocktails` has returned the cocktail's `id`.**

```
compute_alcohol_content(
    cocktail_id=<id from search_cocktails results>,
)
```

Computes the volume-weighted ABV across the cocktail's listed ingredients:

    ABV = sum(volume_ml * abv) / sum(volume_ml)

Non-alcoholic ingredients (juice, syrup, soda) have `abv=0` and pull the
overall ABV down — that's the point. This is the **build ABV** as listed in
the recipe; it does NOT account for ice dilution during shake/stir (which
would lower the served-glass ABV further, typically by 20-30% for shaken
drinks).

Returns:
- `cocktail_id` — echoes the input
- `abv_pct` — rounded to 1 decimal
- `ingredients_summary` — one-line text breakdown
- `answer_summary_block` — grader-ready text; **quote this verbatim** in
  your reply rather than reformatting

If the cocktail_id is unknown, the function returns an `error` envelope —
surface that to the user.
