# Trajectory 2 — memory transfers across sites

The same *class* of breakage on a different site. The episode log holds only `quotes` episodes at this point, and one of shop's ambiguous decisions is resolved from it for free.

*Captured from a live run by `eval/trajectories.py`. Prompts and replies are verbatim; every number is what the tools actually returned.*

## 0. The trigger

`shop` was mutated by `decoy_injection` at severity 2.

> injected 48 decoys on ['product-rating', 'product-price'] marked .was-value (plausible wrong values, ahead of the real node)

The static extractor does not fail. It reports:

| records | fill rate | errors raised | F1 vs frozen truth |
|---|---|---|---|
| 24 | 100% | 0 | **0.80** |

This is a **silent failure**: high fill, wrong values, nothing thrown.

## 1. PERCEIVE — what the monitor saw

No exception is involved. Each signal compares this run to a rolling baseline of healthy runs.

**`price`** — health 0.91 (critical)
- `match_count` (1.00) — locator now matches 2.00 nodes per record (baseline 1.00) -- an extra node is shadowing the real one
- `novel_values` (1.00) — 100% of values never seen in the 24-value baseline sample
- `value_drift` (0.32) — numeric drift: median 173.22 vs 128.31, KS=0.42

**`rating`** — health 0.85 (critical)
- `match_count` (1.00) — locator now matches 2.00 nodes per record (baseline 1.00) -- an extra node is shadowing the real one

## 2. DIAGNOSE — candidates, measured before any model sees them

### field `price`

Structural change: `DECOY_INJECTED`. 8 candidates generated from last-known-good values, each **executed against every record on the page**:

| # | kind | query | recovers | covers | robustness | works on old page |
|---|---|---|---|---|---|---|
| 0 | jsonld | `offers.price` | 1.00 | 1.00 | 0.90 | yes |
| 1 | css | `span.product-price:not(.was-value)` | 1.00 | 1.00 | 0.75 | yes |
| 2 | structural | `./*[3]/*[2]` | 1.00 | 1.00 | 0.45 | no |
| 3 | xpath | `./div[1]/span[2]` | 1.00 | 1.00 | 0.40 | no |
| 4 | regex | `([£$€]\s?\d[\d,]*\.\d{2})` | 0.08 | 1.00 | 0.50 | yes |
| 5 | regex | `\$(\d+\.\d{2})` | 0.08 | 1.00 | 0.50 | yes |
| 6 | css | `span.product-price` | 0.00 | 1.00 | 0.75 | yes |
| 7 | text_anchor | `$112.23` | 0.00 | 0.04 | 0.72 | no |

**Memory recall** (episodes are keyed on a symptom fingerprint stripped of site identity, so a lesson can transfer):

- similar breakage on 'quotes.author' (0.78 match): strategy exclusion -> css 'small.author:not(.compare-at)' -> healed (F1 0.33 -> 1.00, 1 cycle(s))
- similar breakage on 'quotes.text' (0.78 match): strategy exclusion -> css 'span.text:not(.compare-at)' -> healed (F1 0.33 -> 1.00, 1 cycle(s))

**Route taken:** recalled from memory (0 tokens).

**Chosen:** `css span.product-price:not(.was-value)`

> similar breakage on 'quotes.author' (0.78 match): strategy exclusion -> css 'small.author:not(.compare-at)' -> healed (F1 0.33 -> 1.00, 1 cycle(s))

### field `rating`

Structural change: `DECOY_INJECTED`. 8 candidates generated from last-known-good values, each **executed against every record on the page**:

| # | kind | query | recovers | covers | robustness | works on old page |
|---|---|---|---|---|---|---|
| 0 | jsonld | `aggregateRating.ratingValue` | 1.00 | 1.00 | 0.90 | yes |
| 1 | css | `span.product-rating:not(.was-value)` | 1.00 | 1.00 | 0.75 | yes |
| 2 | css | `span.was-value` @data-value | 1.00 | 1.00 | 0.90 | no |
| 3 | css | `span.was-value:not(.product-price)` @data-value | 1.00 | 1.00 | 0.90 | no |
| 4 | structural | `./*[3]/*[3]` @data-value | 1.00 | 1.00 | 0.90 | no |
| 5 | xpath | `./div[1]/span[3]` @data-value | 1.00 | 1.00 | 0.90 | no |
| 6 | css | `span.product-rating` @data-value | 1.00 | 1.00 | 0.90 | yes |
| 7 | css | `span` @data-value | 1.00 | 1.00 | 0.90 | yes |

**Memory recall** (episodes are keyed on a symptom fingerprint stripped of site identity, so a lesson can transfer):

- similar breakage on 'quotes.author' (0.68 match): strategy exclusion -> css 'small.author:not(.compare-at)' -> healed (F1 0.33 -> 1.00, 1 cycle(s))
- similar breakage on 'quotes.text' (0.68 match): strategy exclusion -> css 'span.text:not(.compare-at)' -> healed (F1 0.33 -> 1.00, 1 cycle(s))

**Route taken:** recalled from memory (0 tokens).

**Chosen:** `css span.product-rating:not(.was-value)`

> similar breakage on 'quotes.author' (0.68 match): strategy exclusion -> css 'small.author:not(.compare-at)' -> healed (F1 0.33 -> 1.00, 1 cycle(s))

## 3. The model step

**Not reached.** Memory and the deterministic ranker resolved every decision, so no model was called and no tokens were spent. Across the full matrix this is the majority path: recall 51%, ranker alone 33%, model 16%.

## 4. VERIFY — three gates, all mandatory

| gate | result | what it checks |
|---|---|---|
| `G1-recovery` | **pass** | re-extract on the broken page vs last-known-good |
| `G2-regression` | **pass** | run the patched spec on the page that still worked |
| `G3-clearance` | **pass** | the health signals that fired must go quiet |

## 5. Outcome

**Healed** in 1 cycle(s). Spec `v1 -> v2`. F1 **0.80 -> 1.00** against frozen truth the loop never saw.

Patches are additive — the old locator is demoted, not deleted, because sites A/B test and revert:

```diff
spec shop v1 -> v2  (autoheal: cycle 1: DECOY_INJECTED(2) -> fields.price: promote css 'span.product-price:not(.was-value)' to tier 0 -- recalled: similar breakage on 'quotes.author' (0.78 match): strategy exclusion -> css 'small.author:not(.compare-at)' -> healed (F1 0.33 -> 1.00, 1 cycle(s)); fields.rating: promote css 'span.product-rating:not(.was-value)' to tier 0 -- recalled: similar breakage on 'quotes.author' (0.68 match): strategy exclusion -> css 'small.author:not(.compare-at)' -> healed (F1 0.33 -> 1.00, 1 cycle(s)))
  fields.price:
    + [0] css 'span.product-price:not(.was-value)'   (born v2)
    ~ [0->1] css 'span.product-price'   (demoted, kept as fallback)
    ~ [1->2] jsonld 'offers.price'   (demoted, kept as fallback)
    ~ [2->3] regex '\\$(\\d+\\.\\d{2})'   (demoted, kept as fallback)
  fields.rating:
    + [0] css 'span.product-rating:not(.was-value)'   (born v2)
    ~ [0->1] css 'span.product-rating'   (demoted, kept as fallback)
    ~ [1->2] jsonld 'aggregateRating.ratingValue'   (demoted, kept as fallback)
    ~ [2->3] css 'span.product-rating'   (demoted, kept as fallback)
```

**Model calls:** 0 · **tokens:** 0 · **resolved by recall:** 2
