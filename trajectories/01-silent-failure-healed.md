# Trajectory 1 — silent failure, healed

The flagship case. A decoy is injected ahead of every quote: fill rate stays at 100%, nothing raises, and every value is wrong. The loop detects it, repairs it and verifies the repair.

*Captured from a live run by `eval/trajectories.py`. Prompts and replies are verbatim; every number is what the tools actually returned.*

## 0. The trigger

`quotes` was mutated by `decoy_injection` at severity 2.

> injected 20 decoys on ['author', 'text'] marked .compare-at (plausible wrong values, ahead of the real node)

The static extractor does not fail. It reports:

| records | fill rate | errors raised | F1 vs frozen truth |
|---|---|---|---|
| 10 | 100% | 0 | **0.33** |

This is a **silent failure**: high fill, wrong values, nothing thrown.

## 1. PERCEIVE — what the monitor saw

No exception is involved. Each signal compares this run to a rolling baseline of healthy runs.

**`author`** — health 0.89 (critical)
- `match_count` (1.00) — locator now matches 2.00 nodes per record (baseline 1.00) -- an extra node is shadowing the real one
- `novel_values` (1.00) — 100% of values never seen in the 10-value baseline sample

**`text`** — health 0.89 (critical)
- `match_count` (1.00) — locator now matches 2.00 nodes per record (baseline 1.00) -- an extra node is shadowing the real one
- `novel_values` (1.00) — 100% of values never seen in the 10-value baseline sample

## 2. DIAGNOSE — candidates, measured before any model sees them

### field `author`

Structural change: `DECOY_INJECTED`. 8 candidates generated from last-known-good values, each **executed against every record on the page**:

| # | kind | query | recovers | covers | robustness | works on old page |
|---|---|---|---|---|---|---|
| 0 | css | `small.author:not(.compare-at)` | 1.00 | 1.00 | 0.75 | yes |
| 1 | structural | `./*[3]/*[2]` | 1.00 | 1.00 | 0.45 | no |
| 2 | xpath | `./span[3]/small[2]` | 1.00 | 1.00 | 0.40 | no |
| 3 | css | `[itemprop="author"]` | 0.00 | 1.00 | 0.88 | yes |
| 4 | css | `small[itemprop]` | 0.00 | 1.00 | 0.88 | yes |
| 5 | text_anchor | `by` | 0.00 | 1.00 | 0.72 | no |
| 6 | css | `small.author` | 0.00 | 1.00 | 0.75 | yes |
| 7 | xpath | `.//*[@itemprop="author"]` | 0.00 | 1.00 | 0.40 | yes |

**Route taken:** deterministic top-1 (0 tokens).

**Chosen:** `css small.author:not(.compare-at)`

> css 'small.author:not(.compare-at)' | recovers 100% of known values, covers 100% of records, robustness 0.75, also works on the pre-break page

### field `text`

Structural change: `DECOY_INJECTED`. 7 candidates generated from last-known-good values, each **executed against every record on the page**:

| # | kind | query | recovers | covers | robustness | works on old page |
|---|---|---|---|---|---|---|
| 0 | css | `span.text:not(.compare-at)` | 1.00 | 1.00 | 0.75 | yes |
| 1 | structural | `./*[2]` | 1.00 | 1.00 | 0.45 | no |
| 2 | xpath | `./span[2]` | 1.00 | 1.00 | 0.40 | no |
| 3 | css | `[itemprop="text"]` | 0.00 | 1.00 | 0.88 | yes |
| 4 | css | `span[itemprop]` | 0.00 | 1.00 | 0.88 | yes |
| 5 | css | `span.text` | 0.00 | 1.00 | 0.75 | yes |
| 6 | xpath | `.//*[@itemprop="text"]` | 0.00 | 1.00 | 0.40 | yes |

**Route taken:** deterministic top-1 (0 tokens).

**Chosen:** `css span.text:not(.compare-at)`

> css 'span.text:not(.compare-at)' | recovers 100% of known values, covers 100% of records, robustness 0.75, also works on the pre-break page

## 3. The model step

**Not reached.** Memory and the deterministic ranker resolved every decision, so no model was called and no tokens were spent. Across the full matrix this is the majority path: recall 51%, ranker alone 33%, model 16%.

## 4. VERIFY — three gates, all mandatory

| gate | result | what it checks |
|---|---|---|
| `G1-recovery` | **pass** | re-extract on the broken page vs last-known-good |
| `G2-regression` | **pass** | run the patched spec on the page that still worked |
| `G3-clearance` | **pass** | the health signals that fired must go quiet |

## 5. Outcome

**Healed** in 1 cycle(s). Spec `v1 -> v2`. F1 **0.33 -> 1.00** against frozen truth the loop never saw.

Patches are additive — the old locator is demoted, not deleted, because sites A/B test and revert:

```diff
spec quotes v1 -> v2  (autoheal: cycle 1: DECOY_INJECTED(2) -> fields.author: promote css 'small.author:not(.compare-at)' to tier 0 -- css 'small.author:not(.compare-at)' | recovers 100% of known values, covers 100% of records, robustness 0.75, also works on the pre-break page; fields.text: promote css 'span.text:not(.compare-at)' to tier 0 -- css 'span.text:not(.compare-at)' | recovers 100% of known values, covers 100% of records, robustness 0.75, also works on the pre-break page)
  fields.author:
    + [0] css 'small.author:not(.compare-at)'   (born v2)
    ~ [0->1] css 'small.author'   (demoted, kept as fallback)
    ~ [1->2] xpath './/*[@itemprop="author"]'   (demoted, kept as fallback)
    ~ [2->3] text_anchor 'by'   (demoted, kept as fallback)
  fields.text:
    + [0] css 'span.text:not(.compare-at)'   (born v2)
    ~ [0->1] css 'span.text'   (demoted, kept as fallback)
    ~ [1->2] xpath './/*[@itemprop="text"]'   (demoted, kept as fallback)
```

**Model calls:** 0 · **tokens:** 0 · **resolved by recall:** 0
