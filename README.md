# Autoheal — self-healing web extraction

[![ci](https://github.com/Ilan-07/autoheal/actions/workflows/ci.yml/badge.svg)](https://github.com/Ilan-07/autoheal/actions/workflows/ci.yml)

Scrapers don't fail loudly. A site redesigns, selectors match the wrong node, and the pipeline
writes plausible garbage for three weeks. Autoheal's job isn't extraction — it's **repair**:

```
perceive → diagnose → patch → verify → remember
```

## Status (2026-08-30)

**Stage 2 of 3 complete — the loop closes and heals end-to-end.**
The rule for this build is that nothing agent-shaped gets written until breakage is measurable.
Stage 1 built the ruler; stage 2 built the loop and is scored by it.

| Module | State |
|---|---|
| `autoheal/spec.py` — versioned extractor specs | ✅ |
| `autoheal/runtime.py` — spec × DOM → records + provenance | ✅ |
| `autoheal/metrics.py` — alignment-free multiset P/R/F1 | ✅ |
| `autoheal/perceive.py` — 10 health signals → `BreakageReport` | ✅ |
| `autoheal/diff.py` — DOM alignment + change classification | ✅ |
| `autoheal/localize.py` — known values → ranked candidate locators | ✅ |
| `autoheal/patch.py` — additive, bounded, reversible spec versioning | ✅ |
| `autoheal/verify.py` — G1 recovery · G2 regression · G3 clearance | ✅ |
| `autoheal/memory.py` — store, symptom fingerprints, episode recall | ✅ |
| `autoheal/diagnose.py` — memory → ranker → (optional) model | ✅ |
| `autoheal/loop.py` — the orchestrator, with quarantine | ✅ |
| `eval/` — B0 baseline, perceive eval, end-to-end heal eval, 2 drift lockfiles | ✅ |
| `eval/ablations.py` — −memory · −regression · −diff · −stack · −known-good | ✅ |
| `tests/` — 303 tests: hostile inputs, eval arithmetic, drift-checker failure, demo artefact | ✅ |
| CI — verify · test · hash-seed reproducibility · 2 drift gates · detection/FPR gate | ✅ |
| `eval/b1_oneshot.py` — one-shot baseline vs. the loop | ✅ |
| `autoheal/diagnose.py` — Anthropic **and** Ollama providers, schema-constrained | ✅ |
| `demo/record.py` · `demo/replay.html` — offline, self-contained replay | ✅ |
| recorded video · corpus beyond 4 sites | ⬜ next |

```
make verify      # independent ground-truth checks
make test        # 303 tests
make check       # fail if the committed B0 baseline no longer reproduces
make perceive    # detection rate vs false-alarm rate (non-zero exit on either)
make heal        # end-to-end recovery, ranker accuracy, memory ablation
make ablations   # which parts of the design are load-bearing
make b1          # one-shot baseline (needs a model; not reproducible)
make check-heal  # fail if the committed healing numbers no longer reproduce
make all         # everything
```

CI runs all of it on every push, plus the B0 *and* healing evals under three different
`PYTHONHASHSEED` values — the numbers below are only meaningful if they reproduce,
and that was not true until the stage-1 audit.

## Results (seeds 0–3, fully offline, no model in the loop)

Every number below comes from `make all`. There is no network access and no LLM call
anywhere in it: the ranker decides, and the model step is opt-in (see *Where the model
actually goes*). Spread is real across-seed variance.

**Detection — does the monitor work?**

| | |
|---|---|
| genuinely degraded cases detected | **41/41** (1.00) |
| silent failures caught (≥90% fill, <90% F1) | **14/14** (1.00) |
| false alarms on clean + content-churned pages | **0/32** (0.00) — at warn level too |

**Repair — does the loop heal?**

| | B0 static | Autoheal |
|---|---|---|
| recovery rate on cases needing repair | 0.00 *by construction* | **0.80 – 0.86** |
| mean F1 after breakage | 0.271 – 0.312 | **0.904 – 0.924** |
| mean cycles-to-recover | n/a | **1.00** (cap 4) |
| honest quarantines | n/a | 0.14 – 0.20 |
| healthy cases damaged by the loop | n/a | **0** |

Every quarantine across all four seeds is a `content_deferred` case on a page that ships
no structured data — the values genuinely are not in the DOM any more. Refusing is the
correct answer there, and the loop emits a human-review card saying so rather than
inventing a locator.

**The ranker, with no model at all** — `PLAN.md` calls this the make-or-break number:

| | |
|---|---|
| top-1: a fully-recovering locator is the ranker's first choice | **0.75 – 0.76** |
| top-3 | 0.78 – 0.80 |

Top-1 and top-3 nearly coincide by construction — recovery dominates the score, so a
fully-recovering candidate sorts to rank 0. The number that carries information is
whether such a candidate is generated at all; the ~24% where it is not is dominated by
`content_deferred`, where no locator exists to find.

**Memory — measured, not asserted, and smaller than we wanted.** Sites are visited in a
seed-dependent order so a trend cannot be an ordering artefact.

| recall scope | model calls needed | reduction | recovery |
|---|---|---|---|
| any prior episode | **22 – 29** | **31% – 49%** | 0.80 – 0.86 |
| *different site only* (transfer) | 35 | **19%** | 0.85 |
| memory ablated | 42 – 44 | — | 0.80 – 0.86 |

Two honest readings. Memory does not make the loop more *accurate* — recovery is identical
in every arm — it makes it *cheaper*. And most of that saving is a site breaking the same
way twice, not a lesson moving between sites: isolate cross-site recall and the saving
falls from ~49% to 19%. Transfer is real, and it is the smaller half. Episodes are keyed on
a *strategy class* (`structured_data`, `semantic_attr`, `exclusion`, `positional`, …) rather
than a concrete locator kind specifically to make transfer possible; keying on the kind
scored 12%. Part of the residual gap is not fixable by tuning — `books` ships no structured
data, so it genuinely cannot reuse `shop`'s winning strategy.

## B1 — the one-shot baseline

`make b1`. Same 20 cases, same three gates, same frozen truth. The only difference is
what the model receives: B1 gets the raw broken page, Autoheal gets ~8 candidates that
have each already been executed against every record on it. Both ran on
`gpt-oss:120b-cloud`.

| | B0 static | B1 one-shot | **Autoheal** | Autoheal +model |
|---|---|---|---|---|
| recovery | 0.00 | 0.60 | **0.85** | 0.85 |
| mean F1 after repair | 0.27 | 0.774 | **0.920** | 0.913 |
| tokens, 20 cases | 0 | **500,039** | **0** | 43,587 |
| locators that fail the pre-break page | — | **16/72 (22%)** | 0 | 0 |

B1 is not reproducible and it does swing: two runs of the identical configuration
gave recovery 0.55 / F1 0.840 and 0.60 / 0.774. Autoheal's column is byte-identical
across runs and across three `PYTHONHASHSEED` values, which is the asymmetry the
whole design is arguing for.

**The durability gap is the result, not the recovery gap.** B1's chosen addressing styles
were `positional 22 · hashed_class 20 · stable_class 12 · semantic_attr 8 · exclusion 5 ·
bare_tag 4 · regex_shape 1` — **58% positional paths or hashed CSS-in-JS classes**, the two
least durable styles, the ones `prior()` ranks last. It repeatedly reached for the hashed class the
mutation had just minted: perfect today, worthless at the next deploy. That is what the
28% pre-break-page failure rate measures.

This also rescues the `−regression` null result below. G2 rejects nothing when it is
grading our own ranker, because the ranker does not propose overfits. Against B1 it has
**16 to catch**. The gate is not redundant; our ranker just made it look that way.

**B1 was corrected twice, both times in its favour.** The first run scored it at 0.10
because the harness never offered it the record selector — five cases had F1 1.00, G1 pass,
G2 pass, and failed G3 purely because `<record>` stayed critical, while Autoheal repairs
roots as a matter of course. Two more were counted as failures when they were transport
errors at 0 tokens. Both were our bugs. A baseline that has not been attacked for
unfairness is not a baseline.

**The model step is not currently earning its keep either.** Turning the LLM on inside
Autoheal left recovery unchanged at 0.85 and moved mean F1 from 0.920 to 0.913 — no
measurable gain for 43,587 tokens. On this corpus the deterministic ranker does the work.
That is a claim in the project's favour, not against it: the headline number does not
depend on a model, and we can show it.

## Ablations — including the ones that didn't flatter us

`make ablations`. Every arm is deterministic and offline; none calls a model.

| arm | recovery | F1 after | quarantined | model calls |
|---|---|---|---|---|
| full | **0.85** | 0.920 | 0.15 | 22 |
| −memory | 0.85 | 0.920 | 0.15 | 43 |
| −memory (cross-site only) | 0.85 | 0.920 | 0.15 | 35 |
| −regression | 0.85 | 0.920 | 0.15 | 30 |
| −diff | 0.85 | 0.920 | 0.15 | 22 |
| −stack | 0.85 | 0.916 | 0.15 | 19 |
| **−known-good** | **0.15** | **0.383** | **0.85** | 179 |

**−known-good is the one that moves recovery, and it moves it a long way.** This is Bet 3
tested causally rather than asserted: withhold the last-known-good values and candidates
must be enumerated from every text-bearing node in the record instead of the ~3 carrying a
value we already know, with `recovery` dropped from the ranking. Recovery falls 0.85 → 0.15,
F1 0.920 → 0.383, and 85% of cases quarantine. The 3 that still heal are the ones where a
fallback already in the spec happens to be right — which needs no supervision at all.

It is also the arm that took three attempts to make honest. Withholding the values from
*generation* was not enough: `recovery` was still a tiebreaker in the sort, the shape-derived
regexes were still built from the values, and `survives_old` was computed as
recovery-of-known-good on the pre-break page — so regression-awareness was quietly handing
the signal back. The test that found all three asserts the blind ranking is **invariant to
corrupting the known-good values**, which is the only version of this claim that can't be
fooled.

**This is not the B1 one-shot baseline** and is not offered as one. It measures the
information asymmetry that makes the model's job small, not a model's accuracy at reading
HTML. B1 still wants a real run.

**−stack quantifies the free-degradation claim.** Removing the fallback tiers doesn't make
repairs worse — it makes 6 more breakages *need* one. That is the locator stack's actual
value: fewer repairs, not better ones.

**−regression is a null result and is published as one.** Removing old-page compatibility
from *both* the ranking term and the G2 gate produces zero overfits. The reason is
structural: candidates are ranked on how many *known-good* values they reproduce, and those
values came from the pre-break page, so a high-recovery candidate is already very likely to
work there. G2 did reject a real overfit during development — a positional path that scored
1.00 on the decoy'd page and pointed at the wrong node on the old one — but once the
generator learned to emit exclusion selectors (`span.text:not(.compare-at)`), the gate had
nothing left to catch. It stays, because it is free and it is the check that would catch a
coincidental positional match. We do not claim it is currently earning its keep.

**−diff is also null for recall**, and the mechanism matters before reading anything into
it: with classification off, *every* episode keys on `UNKNOWN`, so the diff component of the
fingerprint matches trivially rather than being lost. What the arm actually shows is that
the fired-signal set alone is enough to key episodes on this corpus. `diff.py` still earns
its place as evidence in the repair prompt and on the dashboard — just not here.

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

## Where the model actually goes

Ten of the eleven modules are marked `[no LLM]` in `PLAN.md`, and the results above were
produced with the model switched off entirely. The model step exists, is
schema-constrained, and is deliberately hard to reach:

1. **Memory first.** If a prior episode with a matching symptom fingerprint proposes a
   strategy, and the ranker has already validated a candidate of that shape *on this page*,
   the repair is made with zero tokens. Memory proposes; the runtime disposes.
2. **The ranker next.** If one candidate wins clearly (score gap ≥ 0.08 and full recovery),
   it is taken. Paying Opus to agree with an unambiguous winner is the reflex this design
   exists to avoid.
3. **The model only on a genuine tie**, and even then it receives ~8 candidates that have
   each already been *executed* against every record on the page — a few thousand tokens,
   not a few hundred thousand. It returns a `choose_locator` tool call, never free text.
   It may also propose a generalisation of the query it picked; that generalisation is
   re-executed and accepted only if it measures at least as well. The model can pick and
   propose. It cannot assert.

Whatever it returns still has to pass all three gates, so a bad answer costs a cycle
rather than producing a wrong repair. `PLAN.md` specifies `temperature=0` for
determinism — that parameter was removed on Claude Opus 5 and now returns a 400, so it is
not used; reproducibility comes from the deterministic ranker instead, which is why the
published numbers can be re-run offline.

## Stage-2 notes

Five things the eval forced, recorded because each one changed a number:

**The monitor was weak on our own flagship case, so it grew a tenth signal.** `PLAN.md`
specifies nine. The first calibration run scored the decoy — the case the whole project
leads with — at 0.42, a *warn*, on value drift and novelty alone. A decoy does not only
change the value; it makes the locator match **two** nodes where it matched one, and the
runtime already knew that and was discarding it. `match_count` (signal 10) promotes the
decoy to 0.85–0.91 critical on every site where it applies. Match multiplicity is recorded
as provenance only — it never changes which value is extracted.

**Every numeric field silently generated zero candidates.** The `money` transform returns
floats, so a known-good `37732000.0` stringified and normalised to `"377320000"` — one
digit longer than anything on the page. No exception, no warning: `population`, `price`
and every other numeric field simply produced an empty candidate list and the page
quarantined. Fixing the normalisation took recovery from 0.50 to 0.75. The project's own
failure mode, again, in our own code.

**The regression gate rejected a fix that was right, and it was correct to.** For a decoy,
the ranker's best idea is a positional path — which recovers 100% on the broken page and
points at the wrong node on the page that used to work. G2 killed it, four cycles running,
and the site quarantined with a correct repair one selector away. The answer was to
generate the selector a human would write: exclude the impostor by the class it carries
and the real node does not (`span.text:not(.compare-at)`), which satisfies both pages.
The marker is drawn per seed from a pool of real "compare at" class names, so the repair
has to derive it: `autoheal/` never names one, and two tests assert both halves — that the
repair adapts to whichever marker a seed chose, and that no marker string appears anywhere
in the repair code.

**G3 could not clear on a field whose stack had just been patched.** Tier shift and match
multiplicity describe *how a locator resolved*; after a patch the baseline holds those
statistics for a locator that no longer exists, so comparing the replacement against them
is apples to oranges. They are waived for patched fields only — an untouched field with a
tier shift still blocks the gate, which is the entire reason signal 2 exists.

**A fallback selector with the wrong cardinality looks healthy and caps every gate.**
`wikitable`'s fallback record selector sweeps in two header rows: 41 records instead of 39,
so recovery tops out at 0.95 and no gate can ever pass. The loop now prefers induction when
a promoted fallback has the wrong count, and induction can address a sibling group by what
it *contains* (`//tr[.//td]`) when no class distinguishes it.

## Hardening pass

A crash sweep over **540 mutated pages** (4 sites × 9 mutators × 3 severities × 5 seeds)
and 12 adversarial inputs across 7 entry points. The mutated pages produced zero failures.
The adversarial inputs produced four bugs worth recording, all of the same family — code
that had never been executed on the input in question.

**An empty page crashed every entry point.** `lxml` raises `ParserError: Document is empty`
on `""` and on whitespace, and all seven of extract / perceive / diff / candidates / induce /
heal raised on it. An empty response is an ordinary production event — a truncated fetch, a
200 with no body, a render that failed — and the correct reading of it is *zero records*,
which fires the monitor loudly. `runtime.parse` now tolerates it, and a blank page ends in an
honest quarantine rather than a stack trace.

**`t_iso_date("2024-13-45")` returned `"2024-13-45"`.** The regex fallback accepted any
`yyyy-mm-dd`-shaped substring without checking it was a date, so month thirteen, day
forty-five flowed downstream as a valid value. This project's own failure mode, in the
transform layer. It now refuses, which falls through to the next locator in the stack.

**`t_money("1 234,56")` returned `1.0`.** A space-separated thousands group — the normal
convention across much of Europe — stopped the number regex at the space and produced a
confident wrong number. Now 1234.56. Relatedly `"1e5"` returned `1.0`; scientific notation
is not a price format and now refuses rather than guessing the mantissa.

**`bool` satisfied the numeric validator**, since `bool` subclasses `int`. Unreachable from
the current transforms, and exactly the kind of thing that becomes reachable later.

**Blanket `except Exception` around selector evaluation was narrowed** to the errors a
malformed *locator* actually raises. Catching everything meant a bug in this repo would be
recorded as "this field is missing" — silent failure, produced by the machinery built to
detect silent failure. Broad handlers remain only around model I/O, where the exception
surface really is open-ended.

A second pass then measured `eval/`, which had never been measured at all — 34%, with the
summariser at zero. **The arithmetic is the claim**, so a wrong denominator there would move
the headline recovery rate with nothing going red. It now has tests, including one asserting
that non-degraded cases stay out of the recovery denominator.

**The drift lockfiles had only ever passed.** A checker that has never failed is not evidence.
Both now have tests that corrupt the committed numbers in a scratch copy and assert a
non-zero exit.

**The retry path had never produced a success.** Cycle counts across the matrix are
`{1: 50, 0: 30, 4: 2}` — every case either heals immediately or runs to the cap, so
"record what lost, exclude it, try something else" was unexercised in the winning direction.
Forcing a gate failure shows it works: the second cycle picks a *different* strategy, the
losing one is written to memory as a failed episode, and the cap ends in a quarantine card.

A third pass closed the rest. The library sits at 93–100% per module. Three things there
are worth naming, because each is a behaviour that could have broken silently:

- **The committed corpus is now pinned to the scripts that generate it.** `author_specs.py`
  and `eval/sites/*/spec.v1.json` could drift apart — edit one and `make truth` would
  regenerate a corpus that no longer matches the published numbers. Both the specs and the
  frozen truth are now asserted against what the scripts produce.
- **Every ablation arm is checked to actually ablate something.** A typo'd kwarg raises, but
  an arm whose value happens to equal the default would silently duplicate `full` and be
  published as a null result.
- **The demo artefact has its own tests**, including the `</script>` break-out regression,
  and an assertion that the recorded acts really do show a silent failure followed by a
  repair that clears all three gates — so the demo cannot drift into narrating numbers the
  system did not produce.

All four eval harnesses are byte-identical across three `PYTHONHASHSEED` values, and CI
checks all four rather than the two it checked before.

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
