# Trajectory 3 — quarantine, the human checkpoint

The case the loop cannot fix: the page's visible text is gone and `books` ships no structured data, so the values genuinely are not in the DOM. Refusing is the correct answer, and the loop hands over to a human instead of inventing a locator.

*Captured from a live run by `eval/trajectories.py`. Prompts and replies are verbatim; every number is what the tools actually returned.*

## 0. The trigger

`books` was mutated by `content_deferred` at severity 2.

> blanked visible text in 20 records (60 nodes); structured data left intact

The static extractor does not fail. It reports:

| records | fill rate | errors raised | F1 vs frozen truth |
|---|---|---|---|
| 20 | 75% | 0 | **0.75** |

## 1. PERCEIVE — what the monitor saw

No exception is involved. Each signal compares this run to a rolling baseline of healthy runs.

**`price`** — health 1.00 (critical)
- `fill_drop` (1.00) — fill 0% vs baseline 100% (floor 95%)

## 2. DIAGNOSE — candidates, measured before any model sees them

### field `price`

Structural change: `CONTENT_CHANGED`. **No candidate locator could be generated at all** — the known-good values are not present anywhere in the new DOM, so there is nothing to point at. An empty candidate list is an honest answer and it is what routes this case to quarantine instead of a guess.

> no candidate locator reproduced any known-good value on the broken page

## 3. The model step

**Not reached.** Memory and the deterministic ranker resolved every decision, so no model was called and no tokens were spent. Across the full matrix this is the majority path: recall 51%, ranker alone 33%, model 16%.

## 4. VERIFY — three gates, all mandatory

**Never reached.** No patch was produced, so there was nothing to verify.

## 5. Outcome

**Quarantined.** No patch cleared the gates, so extraction is paused and a human-review card is emitted rather than a guess being written.

This is the designed answer, not a shortfall: a confident wrong value is worse than an admission of defeat. **This is the human checkpoint** — the loop stops and hands over.

```text
QUARANTINE  books  spec v1
  structural change : CONTENT_CHANGED
  cycles spent      : 1 (cap 4)
  last gate state   : never reached verification
  reason            : no candidate locator recovered any known-good value
  monitor evidence  :
    - [price] fill 0% vs baseline 100% (floor 95%)
  action            : extraction is PAUSED for this site. No records will be
                      written until a human reviews the spec. This is deliberate:
                      stale-but-flagged beats fresh-and-wrong.
```

**Model calls:** 0 · **tokens:** 0 · **resolved by recall:** 0
