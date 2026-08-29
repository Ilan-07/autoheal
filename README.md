# Autoheal — self-healing web extraction

Scrapers don't fail loudly. A site redesigns, selectors match the wrong node, and the pipeline
writes plausible garbage for three weeks. Autoheal's job isn't extraction — it's **repair**:

```
perceive → diagnose → patch → verify → remember
```

## Status (2026-08-29)

**Stage 1 of 3 complete — the eval harness exists and the baseline is measured.**
The rule for this build is that nothing agent-shaped gets written until breakage is measurable.

| Module | State |
|---|---|
| `autoheal/spec.py` — versioned extractor specs | ✅ |
| `autoheal/runtime.py` — spec × DOM → records + provenance | ✅ |
| `eval/mutators.py` — 8 seeded, site-agnostic DOM mutations | ✅ |
| `eval/metrics.py` — alignment-free multiset P/R/F1 | ✅ |
| `eval/harness.py` — B0 static baseline over 4 sites × 11 recipes | ✅ |
| `eval/verify_truth.py` — independent ground-truth checks | ✅ |
| `tests/` — 93 tests | ✅ |
| `perceive · diff · localize · diagnose · patch · verify · memory` | ⬜ next |

```
make verify  # independent ground-truth checks
make test    # 93 tests
make eval    # B0 static baseline
make all     # all three
```

## B0 static baseline (seeds 0–3)

Each seed is byte-reproducible; the spread below is real across-seed variance,
not run-to-run noise.

| | |
|---|---|
| clean pages, mean F1 | **1.000** — no false breakage |
| after breakage, mean F1 | **0.645** (0.642–0.648) over 41 live cases |
| cases needing real repair | **20–21 / 41** |
| absorbed free by the locator stack | 6–7 / 41 |
| silent failures (≥90% fill, <90% F1) | 3–4 |
| recovery | **0.00** — a static scraper cannot repair itself |

## Design commitments

**Extractors are versioned data, not prompts.** A spec is JSON interpreted deterministically.
The agent emits a *patch to a spec*. A healthy site costs nothing to scrape, repairs are
diffable, and no model output is ever executed — transforms come from a whitelist
(`runtime.TRANSFORMS`).

**Each field is a stack of locators, not one selector.** `price` is
`[css, jsonld, regex, structural]`; first value that survives validation wins, and the winning
tier is recorded. Two consequences: some breakages heal at zero cost (6/42 above), and a
**tier shift is a leading indicator of silent breakage** — it fires even when the value still
looks fine.

**Validation failure falls through, it does not emit.** A value that fails its validator is
treated as a miss so the stack keeps walking. Emitting it *is* the failure mode.

**Scoring is alignment-free and precision-sensitive.** Fields are compared as multisets against
frozen ground truth, so reordering is free but inventing a value is punished. A fill-rate metric
scores the decoy case 1.00; F1 scores it 0.60.

## The eval is built not to flatter us

- **Mutators never see the spec.** They locate the repeating record container heuristically and
  deform it. We are not breaking exactly what we know how to fix.
- **`class_rename` targets *component* classes** (`price_color`), identified as tokens carried by
  exactly one text-bearing element per record — not layout chrome (`col-md-3`). An earlier
  frequency-ranked version renamed Bootstrap grid classes and every extractor kept working, which
  would have made the mutation look survivable when it wasn't.
- **`jsonld_drop` exists to defeat our own fallbacks.** Without it, pages with good structured
  data survive almost any redesign via tier 2 and the repair loop is never exercised.
- **`decoy_injection` is the flagship.** It clones a field node, gives it a plausible wrong value
  (a struck-through "compare at" price — a real and common redesign), and inserts it ahead of the
  real one. Nothing errors. Fill rate stays 100%. Every number is quietly wrong.
- **Compound recipes.** A real redesign is never one edit; `full_rewrite` takes all four sites to
  zero records.

## Corpus

| site | source | records |
|---|---|---|
| `books` | books.toscrape.com (scraping sandbox), frozen | 20 |
| `quotes` | quotes.toscrape.com (scraping sandbox), frozen | 10 |
| `wikitable` | real `wikitable` markup from Wikipedia, extracted verbatim | 39 |
| `shop` | synthetic SSR product grid with JSON-LD (`eval/sites/shop/generate.py`) | 24 |

Ground truth is v1 output on the clean page, frozen to `truth.json`. Everything is offline and
seeded; `make eval` is reproducible with no network.

## Stage-1 audit

The harness was audited before any agent code was written. Four defects found and fixed;
they are recorded here because two of them would have silently invalidated the results.

**The eval was not reproducible across processes.** Same seed gave F1 0.658 / 0.648 / 0.653.
`_field_like_tokens` and `decoy_injection` built their token lists by iterating a **`set`**,
whose order depends on `PYTHONHASHSEED`; that order then fed `rng.shuffle`, so the seeded RNG
picked different mutation targets in every process. Fixed by sorting. The existing determinism
test could not catch it — it compared two calls *in the same process*, where set order is
stable. `test_mutations_are_deterministic_across_processes` now runs the mutators under three
different hash seeds in subprocesses.

**`decoy_injection` — the flagship mutation — missed about half the time.** It carried its own
weaker copy of field detection and frequently decoyed layout chrome (`icon-star`, `col-lg-3`)
instead of an extracted field. Now shares `_field_like_tokens`.

**Ground truth was circular.** `truth.json` *is* v1's output on the clean page, so
`test_clean_page_scores_perfectly` asserted `x == x` and could never fail. `eval/verify_truth.py`
now checks truth on a *different* mechanism: raw-text regex over the HTML (all 20 `£` prices in
`books` must equal the 20 extracted prices) and domain invariants (`wikitable` is a ranked list,
so population must be strictly descending — any off-by-one or row swap shows up immediately).

**Our own JSON-LD handling had the exact bug this project is about.** Record roots were aligned
to JSON-LD objects by position with no `@type` filter, so a single leading `WebSite` or
`BreadcrumbList` node — which real pages almost always carry — shifted every record by one and
returned confident, plausible, wrong values. `_align_jsonld` now keeps only the modal `@type`,
and a root/object count mismatch is reported in `run.errors` instead of being indexed past.

One check was *supposed* to fail and did: `not_constant` fired on `books.availability`, because
every book on that page really is "In stock". Carried forward as a design note for
`perceive.py` — collapse must be judged against a baseline. A field that has always been
constant is not evidence; a field that *newly* becomes constant is.

See `PLAN.md` for the full architecture and the remaining schedule.
