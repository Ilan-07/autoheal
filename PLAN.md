> **This is the plan as written before any code existed, kept unedited on purpose.**
>
> It is the baseline the Improvement Changelog in [README.md](README.md#6-improvement-changelog)
> measures against, so several things in here were later contradicted by measurement and are
> *deliberately* left wrong: it specifies nine health signals (there are ten), four sites (there
> are six), `temperature=0` (removed on Claude Opus 5, returns a 400), and a cycles-to-recover
> curve that turned out to be flat and was retired. **Read README.md for what the project
> actually does and scores.** This file is here so you can see what changed and why.

---

# Autoheal — Self-Healing Extraction

**Deadline: 2026-08-31 EOD. Today: 2026-08-29.** ~2.5 working days.

The thesis in one line: *a scraper that fails silently is a monitoring problem before it is
an extraction problem, and the fix is a closed loop — perceive → diagnose → patch → verify →
remember — where memory is load-bearing, not decorative.*

---

## 0. The three non-obvious bets

Everything below follows from these. If a judge remembers three things, it's these.

**Bet 1 — Extractors are versioned data, not prompts.**
An extractor is a declarative spec (JSON) interpreted by a deterministic runtime. The agent's
output is a *patch to a spec*, not a fresh selector string and not generated Python. This buys:
no LLM in the steady-state hot path (a healthy site costs $0 to scrape), diffable/auditable
repairs, per-field validators, and a memory substrate that is literally the artifact itself.

**Bet 2 — Each field is a *stack* of locators, not one selector.**
`price` is not `.product-price`. It's an ordered list: `[css .product-price, jsonld
offers.price, text-anchor "Price:"→next, xpath //td[2], structural path-from-anchor]`. First
one that passes validation wins, and we record *which* one won.

Two consequences:
- **Free graceful degradation.** A class rename is often absorbed by fallback #2 with zero LLM
  calls. Real self-healing systems shouldn't call a model for every hiccup.
- **A leading indicator.** If `price` was served by locator #0 for 30 runs and today it's
  locator #3, that's a breakage signal *even when the extracted value looks perfectly fine*.
  This is the sharpest silent-failure detector we have and nothing else in the space has it.

**Bet 3 — Known-good records are the supervision signal for repair, not just the test.**
The one-shot baseline says "here's 400KB of new HTML, fix the selector." We instead say: "you
extracted `$24.99` for `price` yesterday, with the surrounding text `Add to basket`. Find
`$24.99` in the new DOM. Here are the 4 nodes containing it, their paths, and the structural
diff class that moved them." The LLM then does a *small, constrained* task — choose and
generalize a robust locator — instead of reading a haystack. This is the single biggest
accuracy and token-cost win in the design.

---

## 1. Architecture

```
                       ┌─────────────────────────────────────┐
   snapshot ──────────▶│  RUNTIME  spec ⨯ DOM → records       │──▶ records + provenance
   (frozen HTML)       │  locator stack, per-field validators │    (which locator won)
                       └─────────────────────────────────────┘
                                       │
                                       ▼
   baselines ─────────▶┌─────────────────────────────────────┐
   (rolling stats)     │  PERCEIVE   health monitor           │──▶ BreakageReport
                       │  9 signals, no exceptions needed     │    (per-field, w/ evidence)
                       └─────────────────────────────────────┘
                                       │ fires
                                       ▼
   last-known-good ───▶┌─────────────────────────────────────┐
   snapshot + records  │  DIAGNOSE   structural DOM diff      │──▶ ranked candidate locators
   repair episodes ───▶│  + value re-localization + recall    │    + hypothesis (mutation class)
                       └─────────────────────────────────────┘
                                       │
                                       ▼
                       ┌─────────────────────────────────────┐
                       │  PATCH   spec v(N) → v(N+1)          │──▶ additive: new locator to
                       │  LLM picks/generalizes, never eval() │    head, old demoted to fallback
                       └─────────────────────────────────────┘
                                       │
                                       ▼
                       ┌─────────────────────────────────────┐
                       │  VERIFY   3 gates, all must pass     │──┐ fail → back to DIAGNOSE
                       │  new-snap F1 · old-snap regression · │  │        (counts a cycle)
                       │  health signals cleared              │  │
                       └─────────────────────────────────────┘  │
                                       │ pass                   │
                                       ▼                        │
                       ┌─────────────────────────────────────┐  │
                       │  REMEMBER   commit spec + episode    │◀─┘ failures are remembered too
                       │  symptom fingerprint → strategy      │
                       └─────────────────────────────────────┘
```

### 1.1 Extractor spec (the core data structure)

```jsonc
{
  "site": "shop-a", "version": 7, "parent": 6,
  "record_selector": {                       // how to find repeating record roots
    "stack": [
      {"kind": "css",     "q": "article.product"},
      {"kind": "jsonld",  "q": "$[?@.@type=='Product']"},
      {"kind": "repeat",  "q": "auto"}       // structural repeated-subtree induction
    ]
  },
  "fields": {
    "price": {
      "stack": [
        {"kind": "css",         "q": ".product-price", "born": 1, "last_hit": 42},
        {"kind": "jsonld",      "q": "offers.price",   "born": 1, "last_hit": 42},
        {"kind": "text_anchor", "q": "Price:", "rel": "next_text"},
        {"kind": "xpath",       "q": ".//td[2]"}
      ],
      "transform": "money",                  // whitelisted named transforms, NOT eval()
      "validators": [
        {"type": "number", "min": 0.01, "max": 100000},
        {"type": "required", "rate": 0.95}   // ≥95% of records must have it
      ]
    }
  }
}
```

Locator kinds: `css` · `xpath` · `jsonld` (schema.org / embedded JSON) · `microdata` ·
`text_anchor` (label → sibling/parent-relative) · `structural` (path relative to a stable
anchor node, tag-only, class-free) · `regex` (on node text) · `attr` (`data-*`, `itemprop`).

Transforms are a **fixed whitelist** (`money`, `int`, `iso_date`, `strip`, `url_join`,
`text`). No LLM-generated code is ever executed. This is a deliberate safety + reviewability
choice and worth saying out loud in the demo.

### 1.2 PERCEIVE — nine signals

Runs on every extraction, compares to a rolling baseline. **No exception is required to
fire** — that's the whole point.

| # | Signal | Catches |
|---|--------|---------|
| 1 | Field fill-rate vs. baseline | selector matches nothing |
| 2 | **Locator-tier shift** (which stack entry won) | *silent* rename, absorbed by fallback |
| 3 | Record count per page | reparenting, pagination change |
| 4 | Validator pass-rate | type/format drift |
| 5 | Value-distribution drift (numeric: median+IQR shift & KS; string: length dist + token Jaccard vs. baseline sample; categorical: total-variation distance) | wrong node, wrong units |
| 6 | **Constant-collapse / duplicate rate** | selector now hits a nav/header element → every row identical |
| 7 | Cross-field invariants (`price>0`, `date<=now`, `title != site_name`) | plausible-garbage |
| 8 | Novel-value rate (fraction of values never seen before) | content vs. structure change |
| 9 | Cross-record shape entropy (fields present per record) | partial breakage |

Signals → weighted per-field health score → `BreakageReport{field, score, evidence[], severity}`.
Evidence is human-readable strings; they go verbatim into the diagnose prompt and onto the
demo dashboard. **False-alarm rate on unmutated pages is an eval metric** — a monitor that
cries wolf is worthless, and showing near-zero FPR is a credibility win.

### 1.3 DIAGNOSE — the part that beats one-shot

1. **Structural diff.** Align last-known-good DOM against the new one (tag+position+text
   hashing, then greedy subtree matching). Emit a typed edit script and classify it:
   `CLASS_RENAME` · `WRAPPER_INSERTED` · `TAG_SWAP` · `SUBTREE_MOVED` · `ATTR_DROPPED` ·
   `CONTENT_DEFERRED` (moved into `<script>`/`<template>`) · `PAGINATION_CHANGED`.
2. **Value re-localization.** For each broken field, take the last-known-good values, search
   the new DOM for exact/fuzzy matches, and derive candidate locators for each hit node
   (css/xpath/structural/text-anchor variants, ~6 per node).
3. **Candidate ranking (deterministic, pre-LLM).** Score by: does it recover ≥N known values ·
   does it generalize across all record roots · robustness prior (semantic attrs and
   `itemprop`/`data-*` > stable class names > hashed classes like `css-1a2b3c` > positional
   nth-child) · does it survive the *old* snapshot too.
4. **Memory recall.** Look up the symptom fingerprint (signals fired ⨯ diff class ⨯ field type)
   in the repair episode log. If a prior episode matches, its winning strategy is injected as a
   prior — often skipping the LLM call entirely.
5. **LLM step** (`claude-opus-5`): input is the evidence list + diff classification + top ~8
   ranked candidates + 3 known-good records. Output is a structured choice + generalization,
   via tool-use schema, not free text. Typically ~4–8K tokens in, not 400K.

### 1.4 VERIFY — three gates, all mandatory

- **G1 recovery:** re-extract on the broken snapshot; field-level F1 vs. known-good records ≥ τ.
- **G2 regression:** run the *patched* spec against the *pre-mutation* snapshot. A good patch
  must not break the page that worked. Free, deterministic, and it kills the class of "fix"
  that overfits to today's DOM. Judges love this one.
- **G3 clearance:** re-run PERCEIVE; the signals that fired must now be quiet, and no new
  signal may fire.

Fail → the failure report becomes new evidence and we loop (this increments *cycles-to-recover*,
our headline metric). Hard cap 4 cycles, then quarantine the site and emit a human-review card.

### 1.5 REMEMBER — memory as mechanism

```
store/
  extractors/{site}/v{N}.json      # versioned specs w/ lineage (parent pointer)
  snapshots/{site}/{run}.html.gz   # last-known-good + the one that broke
  records/{site}/{run}.jsonl       # output + per-field provenance (locator tier that won)
  baselines/{site}.json            # rolling stats for PERCEIVE
  episodes.jsonl                   # symptom fingerprint → diagnosis → patch → outcome
```

`episodes.jsonl` is the transferable asset. It's keyed by **symptom fingerprint**, not by site,
so a repair learned on site A is retrievable when site D breaks the same way.

> **The measurable claim:** cycles-to-recover and tokens-per-repair *decrease* over the course
> of an eval run as episodes accumulate. Same mutation class, different site, cheaper fix.
> That is memory being the mechanism, and it's a chart, not an assertion. Randomize site order
> across seeds so it isn't an ordering artifact.

---

## 2. Eval harness (build this FIRST, before any agent code)

Fully offline, seeded, reproducible, zero network.

- **Corpus: 10 frozen sites.** Suggested mix — books.toscrape.com, quotes.toscrape.com (both
  explicitly scraping sandboxes), a Wikipedia table, an HN-style listing, a docs changelog, a
  GitHub-releases-style page, a job-board mock, a real-estate mock, a JSON-LD-heavy product
  page, an infinite-scroll SPA-ish page. Save to disk once; the harness never hits the network.
- **Ground truth:** authored per site (v0 extractor output, then hand-verified and frozen).
- **Mutators** — composable, seeded, severity 1–3:
  1. `class_rename` — semantic → hashed (`.product-price` → `.css-1a2b3c`)
  2. `tag_swap` — `<table>` → div/grid, `<ul>` → divs
  3. `reparent` — insert wrapper nodes, move field into a new span
  4. `attr_migration` — drop `data-testid`, `id` → `data-*`
  5. `pagination_change` — rel/next scheme, `?page=` → `?offset=`
  6. `lazy_load` — content moved into `<template>` or a `<script type=application/json>` blob
  7. **`decoy_injection`** — inject a nav element that *also* matches the old selector, so the
     old extractor keeps producing plausible-but-wrong output. **This is the flagship case:**
     the static baseline scores high "fill rate" and 0 F1. Lead the demo with it.
  8. `text_format_shift` — `$24.99` → `USD 24,99`
- **Metrics:** post-breakage field-F1 and record exact-match · **cycles-to-recover** ·
  silent-failure **detection rate** · **false-alarm rate on unmutated pages** · tokens & $ per
  repair · wall-clock per repair.
- **Comparisons:**
  - B0 static scraper (0% recovery — the floor)
  - B1 one-shot "here's the new HTML, fix the selector" (same model, same budget)
  - **Ablations** (more persuasive than the baselines): −memory · −verify-loop (accept first
    patch) · −structural-diff · −locator-stack (single selector).
- `make eval` → one command, writes `results/{seed}/report.json` + a markdown table.

---

## 3. Demo — the money shot (target 3 minutes)

Split screen. Left: the live page served by a local server. Right: the Autoheal dashboard
(health gauges, record stream, agent trace, spec diff).

- **Act I (45s) — silent failure.** Show healthy extraction. Hit a key: `decoy_injection` fires
  live on the served page. Static baseline pane keeps writing records — *fill rate 100%, all
  wrong*. This is the emotional hook: nothing errored.
- **Act II (75s) — the heal.** Autoheal's health panel goes red with *named evidence*
  ("`price` locator tier 0→3", "duplicate rate 4%→91%"). Agent trace streams: diff class →
  candidates → patch. Verify gates flip green one by one, including the regression gate. Show
  the spec diff `v7 → v8` — a human-readable JSON diff, not a wall of code.
- **Act III (45s) — memory.** Apply the *same mutation class* to a different site. It heals in
  one cycle, using the recalled episode, with a fraction of the tokens. Cut to the
  cycles-to-recover curve trending down. Land the line: *"the second one was cheap because it
  remembered the first."*
- **Backstop:** record the full demo to video by Aug 31 midday. Live demos die. Also ship
  `make demo-replay` that replays a recorded event log into the same dashboard — pixel-identical
  and network-free.

---

## 4. Stack

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.14 + `uv` | `lxml`/`cssselect` ecosystem; uv is already installed |
| Parsing | `lxml` (+ `cssselect`) | needs both XPath and CSS; fast, battle-tested |
| Specs | `pydantic` v2 | validation, JSON round-trip, free schema for tool-use |
| Agent | `anthropic` SDK — `claude-opus-5` for diagnose/patch, `claude-haiku-4-5` for cheap classification/summarization | Opus where reasoning matters, Haiku where it doesn't |
| Structured output | tool-use schemas, not free-text parsing | reliability |
| Dashboard | FastAPI + SSE + one static HTML page (no build step) | zero framework risk at 2am |
| Storage | plain files + JSONL | inspectable on camera; a DB is a liability here |
| Determinism | every run seeded; snapshots frozen; `temperature=0` | reviewers can rerun it |

> Before writing any Anthropic API call, load the `claude-api` skill for current model IDs,
> tool-use shape, and caching. Don't write it from memory.

Cost control: cache the static parts of the diagnose prompt; cap candidates at 8; hard token
ceiling per repair with a budget counter in the report.

```
autoheal/
  spec.py         # pydantic models: ExtractorSpec, Locator, Validator
  runtime.py      # spec ⨯ DOM → records + provenance     [no LLM]
  perceive.py     # 9 signals → BreakageReport            [no LLM]
  diff.py         # DOM alignment + edit script + classification  [no LLM]
  localize.py     # known-value → candidate locators + ranking    [no LLM]
  diagnose.py     # memory recall + LLM hypothesis        [LLM]
  patch.py        # spec v(N) → v(N+1), additive          [LLM, schema-constrained]
  verify.py       # G1/G2/G3 gates                        [no LLM]
  memory.py       # store, fingerprints, episode recall
  loop.py         # the orchestrator
eval/
  sites/          # 10 frozen snapshots + ground truth
  mutators/       # 8 seeded DOM mutations
  harness.py      # runs matrix, emits report.json
demo/
  server.py       # serves a snapshot, mutates on keypress
  dashboard.html  # SSE dashboard
```

Note how much is marked `[no LLM]`. That's the engineering point: the agent is the small,
constrained part of a mostly-deterministic system. Say this to judges — it inverts the usual
"wrap an LLM in a for-loop" impression.

---

## 5. Schedule

Hard rule: **the eval harness exists before the agent does.** If you can't measure healing you
can't demo it, and you'll spend Sunday night arguing with a prompt instead of a number.

### Sat Aug 29 — remainder of today: *"we can measure breakage"*
- [ ] `uv init`, deps, `spec.py`, `runtime.py` (locator stack + validators + provenance)
- [ ] Freeze 4 sites (do all 10 later; 4 is enough to build against), author ground truth
- [ ] Mutators 1–3 + `decoy_injection` (do the flagship one early)
- [ ] `eval/harness.py` + B0 static baseline
- **Gate: `make eval` prints an F1 table showing the static scraper at ~0 post-mutation.**
  If you don't hit this gate tonight, cut sites to 3 and keep going — do not start the agent
  without it.

### Sun Aug 30 — *"it heals end-to-end"*
- **Morning:** `perceive.py` (all 9 signals) + `memory.py` store. Gate: breakage report fires
  on all 4 mutated sites and stays silent on the unmutated ones.
- **Midday:** `diff.py` + `localize.py` + ranking. Gate: for ≥1 mutation class, the correct
  locator is in the top-3 candidates *with no LLM at all*. This is the make-or-break moment of
  the whole project — a strong candidate ranker makes the LLM step easy and a weak one makes it
  impossible. Budget real time here.
- **Afternoon:** `diagnose.py` + `patch.py` (schema-constrained) + `verify.py` 3 gates + `loop.py`.
- **Evening gate: 4 sites × 4 mutations heal end-to-end.** Add B1 one-shot baseline.
- **Night:** expand to 10 sites and all 8 mutators; run the full matrix while you sleep.

### Mon Aug 31 — *"it's provable and it's watchable"*
- **09:00–12:00:** ablations (−memory, −verify, −diff, −stack). Fix whatever the matrix exposed.
  Generate the results table and the cycles-to-recover curve.
- **12:00–15:00:** demo server + dashboard + `make demo-replay`. **Record the video by 15:00.**
- **15:00–17:00:** README (architecture diagram + results table up top), rehearse the 3-minute
  script out loud 3×, time it.
- **17:00 — CODE FREEZE.** After this: only README, slides, rehearsal. Nothing else.

### Cut lines, in order (pre-decided, so you don't debate at 1am)
1. 10 sites → 6 sites. (Cheap, barely noticed.)
2. 8 mutators → 5, keeping `decoy_injection`, `class_rename`, `tag_swap`, `lazy_load`, `reparent`.
3. Ablations → keep only **−memory** (it's the one that defends the thesis).
4. Live-mutation demo → recorded video + replay mode.
5. `lazy_load`/`pagination_change` mutators (the fiddliest to implement) — drop last.

**Never cut:** the verify loop, the locator stack, the episode memory, the false-alarm metric.
Those four *are* the project.

---

## 6. Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| DOM diff rabbit hole (tree-edit-distance is a PhD topic) | **High** | Timebox to 3h. Fall back to a shallow heuristic: match by text-content hash + tag path. It only needs to be good enough to *classify* the change, not to be optimal. |
| Ground-truth authoring eats hours | High | Generate with v0 extractor, spot-check 10 records/site, freeze. Don't hand-type. |
| LLM returns unusable locators | Medium | It never invents from scratch — it *picks* from ranked candidates. Worst case, the deterministic top-1 is the fallback and it still heals. |
| Verify loop oscillates | Medium | Hard cap 4 cycles → quarantine + human-review card. A clean "I couldn't fix this, here's why" is a *feature* to demo, not a failure. |
| Demo breaks live | Medium | Recorded video + `make demo-replay` from an event log. |
| Over-scoping the dashboard | **High** | One static HTML file, SSE, no build step. Timebox 2h. Ugly-but-legible beats pretty-but-broken. |
| Model/API flakiness on stage | Low | Replay mode needs no network at all. |

---

## 7. What "top-notch engineering" means for the reviewer

Concretely, the things to make visible in the README and the repo:

1. **The LLM is the small part.** 7 of 10 modules are marked `[no LLM]` and are unit-tested.
2. **No `eval()` of model output, ever.** Whitelisted transforms; specs are data.
3. **Patches are additive and reversible.** Full lineage `v1→v8`; old locators are demoted, not
   deleted, because sites A/B test and revert.
4. **Verification includes a regression gate** against the last-known-good page — the check that
   distinguishes a repair from an overfit.
5. **Failure is a first-class output.** Quarantine + human-review card beats silent wrongness;
   that's the entire premise of the project, honored in the design.
6. **Every number is reproducible offline** with a seed and frozen snapshots.
7. **The memory claim is measured, not asserted** — cycles-to-recover trends down, with the
   ablation to prove it.
