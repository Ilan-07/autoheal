# Autoheal — self-healing web extraction

[![ci](https://github.com/Ilan-07/autoheal/actions/workflows/ci.yml/badge.svg)](https://github.com/Ilan-07/autoheal/actions/workflows/ci.yml)

**A scraper that breaks loudly is a nuisance. A scraper that breaks quietly is a data-quality
incident you find out about three weeks later.** Autoheal detects the quiet kind and repairs it,
with the model doing the small part.

```
perceive → diagnose → patch → verify → remember
```

| | |
|---|---|
| **Primary metric** — recovery on breakages that degraded extraction | **0.87** vs **0.63** one-shot LLM vs **0.00** static |
| Silent failures detected | **26 / 26** |
| False alarms on healthy pages | **0 / 48** |
| Tokens to produce the headline result | **0** |
| Reproducible | byte-identical across 4 seeds × 3 `PYTHONHASHSEED` values |

Everything below is produced by `make all` from a clean checkout, offline, in about two minutes.

### Where the four deliverables are

| | |
|---|---|
| **1. Solution code + Improvement Changelog** | this repo · changelog in [§6](#6-improvement-changelog) |
| **2. Reproduction guide** | [§8](#8-reproduction-guide) — clean environment, exact commands, versions, runtimes |
| **3. Solution video** (4:05) | [`demo/autoheal-demo.mp4`](demo/autoheal-demo.mp4) |
| **4. Agent trajectories** | [`trajectories/`](trajectories/) — four live runs, prompts verbatim |

Also here: [`PLAN.md`](PLAN.md) is the plan written *before* any code, kept unedited as the
baseline the changelog measures against. The main failure mode is [§9](#9-main-failure-mode) and
the hot take is [§10](#10-hot-take).

---

## 1. Who has this problem

**Anyone who runs scrapers in production and makes decisions on the output.** Price-intelligence
and market-research teams, data engineers maintaining ingestion pipelines, researchers with
long-running collection jobs, and the on-call engineer who owns the pipeline at 2am.

Concretely: a small data team maintaining 50–500 extractors against sites they do not control.
Nobody on that team wrote the sites, nobody is told when they change, and the extractors are the
team's product.

## 2. The bottleneck

Web extraction fails in three ways, and only one of them is handled well today.

| failure | today's tooling | reality |
|---|---|---|
| The request fails | retries, alerts | solved |
| The selector matches nothing | fill-rate alarms | mostly solved |
| **The selector matches the wrong node** | **nothing** | **the expensive one** |

The third case produces **no exception, no empty result and no dropped rows**. A site adds a
struck-through "compare at" price above the real one; your `.price` selector now matches it
first. Fill rate stays at 100%. Record count is unchanged. Every dashboard is green and every
number is wrong.

Two costs follow. **Detection latency:** nobody notices until a human eyeballs the data or a
downstream number looks strange — routinely weeks. **Repair toil:** when it is noticed, an
engineer opens devtools, diffs the markup, rewrites a selector, and re-runs the backfill. That
work is unplanned, interrupt-driven, and repeats every time the site ships a redesign.

This project's claim is that **the detection half is the harder and more valuable half**, and
that once you can detect reliably, most repairs do not need a model at all.

In this corpus, **6 of 61 breakages are silent** — high fill rate, wrong values, nothing thrown.
Those are the ones a fill-rate alarm cannot see.

---

## 3. Quickstart — from nothing to the headline number

Three commands, about two and a half minutes, on a machine with none of this installed. No API
key, no network access, no accounts.

**1. Install `uv`** (the only prerequisite — it fetches the right Python for you):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # macOS / Linux
# Windows PowerShell:  irm https://astral.sh/uv/install.ps1 | iex
# or, if you prefer:   brew install uv   /   pipx install uv
```

**2. Clone and install:**

```bash
git clone https://github.com/Ilan-07/autoheal.git && cd autoheal
uv sync
```

You do **not** need Python 3.13 already installed. `uv sync` reads `.python-version`, downloads
the right interpreter into a local virtualenv, and installs the three runtime dependencies
(`lxml`, `pydantic`, `cssselect`). Nothing is installed system-wide.

**3. Run everything:**

```bash
make all
```

That runs ground-truth verification, 375 tests, the static baseline, the detection eval, the
healing eval and both drift lockfiles. **You should see this at the end**, and the command exits
non-zero if any of it fails:

```
RECOVERY             : 0.87   (B0 static baseline: 0.00 by construction)
mean F1              : 0.289 -> 0.914
healthy-case damage  : 0
baseline reproduces exactly: 72 cases match results/seed0/b0_static.json
healing results reproduce exactly: recovery 0.87, 30 cases needing repair
```

### No `make`? Run the same six steps directly

`make` is not on a stock Windows install. Every target is a single command:

```bash
uv run python -m eval.verify_truth        # independent ground-truth checks
uv run pytest -q                          # 375 tests
uv run python -m eval.check_baseline      # B0 drift lockfile
uv run python -m eval.perceive_eval --seed 0   # detection vs false alarms
uv run python -m eval.heal_eval --seed 0       # recovery, ranker, memory ablation
uv run python -m eval.check_heal          # healing drift lockfile
```

### See it rather than read it

```bash
open demo/autoheal-demo.mp4     # 4:05 narrated walkthrough  (xdg-open on Linux)
open demo/replay.html           # step through the same run yourself — no server, no network
```

`replay.html` is a single self-contained file: double-click it. Space plays, arrow keys step.

### If something goes wrong

- **`uv: command not found`** after installing — open a new shell, or `source ~/.bashrc` / `source ~/.zshrc`.
- **A drift lockfile fails** — that is the check working: it means a number moved. `git status`
  will show which `results/` file differs, and the failure output names the exact metric.
- **`make ablations` or `make b1` looks stuck** — they are the slow ones (~4 min and ~10 min).
  Neither is needed for anything in §5. `make b1` additionally needs a model; see §8.

Full reproduction guide, versions, runtimes and the optional model steps in [§8](#8-reproduction-guide).

---

## 4. The agent solution, and why each choice earns its place

An extractor is a **versioned JSON spec** interpreted by a deterministic runtime. The agent's
output is a *patch to that spec* — never a generated selector string, never generated code.

### 4.1 Better context: known-good values as the supervision signal

The naive framing is "here is 400KB of new HTML, fix the selector." Autoheal instead says:
*yesterday this field returned `£24.99`; find `£24.99` in today's DOM, derive every reasonable way
to address the node it landed in, and score each one by executing it against every record on the
page.* The model, if it is consulted at all, chooses among ~8 pre-measured options.

**Evidence it matters:** with this removed (`−known-good`), the same system recovers **0.13**
instead of 0.87 — by far the largest effect in the ablation table. Autoheal's own score is 0.87;
0.13 is the counterfactual.

### 4.2 Tools: a locator *stack*, not a selector

`price` is not `.product-price`. It is an ordered stack — `[css, jsonld, regex, structural]` —
and the first value that survives validation wins. **Which tier won is recorded.**

**Evidence:** ablating it (`−stack`) leaves recovery about the same but raises the number of
breakages that *need* a repair from 30 to 40. The stack's value is fewer repairs, not better ones.

### 4.3 Verification: three mandatory gates

| gate | checks |
|---|---|
| **G1 recovery** | re-extract on the broken page against last-known-good |
| **G2 regression** | run the patched spec against the page that *still worked* |
| **G3 clearance** | the health signals that fired must go quiet |

G2 is the one that distinguishes a repair from an overfit. **Evidence:** it rejects nothing when
grading our own ranker — reported below as a null result — but against the one-shot baseline it
has **34 overfits to catch**.

### 4.4 Memory: episodes keyed on symptom, not on site

`episodes.jsonl` stores *symptom fingerprint → strategy class → outcome*, deliberately stripped of
site identity, so a repair learned on one site is retrievable when a different site breaks the
same way. Failures are stored too, so the loop does not re-propose a strategy that already lost.

**Evidence:** **68% fewer** decisions need a model. Restricting recall to *other sites only* still
gives 58% — transfer accounts for most of the saving.

### 4.5 Orchestration: a loop with an honest exit

Four cycles maximum, then **quarantine**: extraction pauses and a human-review card is emitted.
Refusing is a first-class output. **This is the human checkpoint** required by ground rule 4/5 —
no repair is ever silently accepted, and an unfixable page stops the pipeline rather than filling
it with plausible garbage.

### 4.6 Where the model actually goes

Measured over a full matrix run, per field-level decision:

| route | share |
|---|---|
| memory recall — 0 tokens | **51%** |
| deterministic ranker decided alone | **33%** |
| ambiguous → model | **16%** |

A model is reached on roughly **one decision in six**, and only after memory and the ranker both
decline. It returns a schema-constrained tool call choosing an index; it never sees raw HTML. If
it proposes a widened query, that query is **re-executed** and kept only if it measures at least
as well. A missing, unreachable, or wrong answer falls back to the ranker, and all three gates
still run. **No model output is ever executed** — transforms come from a fixed whitelist.

### 4.7 Is this an agentic system? The honest answer

A model is reached on one decision in six, and turning it on changes recovery by nothing. It
would be easy to dress that up. Instead:

> **We built the loop, measured it, and found the deterministic parts carry it. We are reporting
> that rather than hiding it. The agent capabilities that *do* earn their place are memory,
> verification and context engineering — and we can prove it, because ablating each one moves
> the number. What we can also prove is that the LLM is not secretly doing the work, which
> almost no agent demo can say.**

Each of those claims is a row in the ablation table below:

Each row below is a **counterfactual**: what the project would score with that capability
removed. Autoheal itself scores 0.87 — the low numbers are the versions of it that do not exist.

| capability | remove it, and… | |
|---|---|---|
| context — known-good values as supervision | recovery **falls to 0.13** | from 0.87 |
| memory — episodes keyed on symptom | model calls **rise to 65** | from 21 |
| verification — the three gates | null on our own ranker | but **34 overfits** caught in B1 |
| tools — the locator stack | **10 more breakages** need a repair | 40 instead of 30 |

By the strict definition — a model autonomously choosing actions in a loop — this is a
constrained workflow, not an autonomous agent. Control flow is fixed, cycles are flat at one,
and the model picks an index from options that were already executed and scored. By the
definition this brief uses — *"better context or better tools… memory to carry important
information forward… verification… orchestration"*, judged on whether each choice **helped** —
every component is here and every one has a measured effect. We would rather be the project
that knows which half is doing the work.

---

## 5. Measured improvement

Same 30 cases, same three gates, same frozen ground truth, same model for both agent arms.

| metric | B0 static (baseline) | B1 one-shot LLM | **Autoheal** | change vs B0 |
|---|---|---|---|---|
| **Primary: recovery** | 0.00 | 0.63 | **0.87** | **+0.87** |
| Mean F1 after breakage | 0.29 | 0.795 | **0.914** | +0.62 |
| Locators that fail the pre-break page | — | 34 / 94 (36%) | **0** | — |
| Silent failures detected | 0 | — | **26 / 26** | — |
| False alarms on healthy pages | — | — | **0 / 48** | — |
| Cost per repair *(tokens)* | 0 | 19,644 | **0** | — |
| Cost per repair *(USD, Opus list rates)* | $0 | ~$0.11 | **$0** | — |
| Wall clock per repair | n/a | 16.1 s | **< 1 s** | — |
| Human time per repair | see below | see below | see below | — |

**On the statistics.** Exact McNemar over the 30 paired cases: Autoheal wins 7 that B1 loses,
B1 wins 0 that Autoheal loses, **p = 0.0156**. Wilson 95% CIs: 0.70–0.95 and 0.46–0.78.

**On cost.** Both agent arms ran on `gpt-oss:120b-cloud`, so actual spend was **$0**. The USD
column converts measured token counts at `claude-opus-5` list rates for comparability. Autoheal's
published results use **zero tokens** — the deterministic path resolves everything; with the model
enabled it spends ~1,600 tokens per call on ~1 decision in 6.

**On human time — this is an estimate, not a measurement, and is labelled as such.** We did not
run a human-baseline study. What we *did* measure is that 6 of 61 breakages are silent, so the
honest statement is about detection latency rather than repair minutes: a silent failure is
invisible to fill-rate alarms and is found only when someone inspects the data. Autoheal detects
all 26 silent cases across seeds at the next extraction run. Repair time itself (open devtools,
diff markup, rewrite selector, re-run backfill) we estimate at 15–45 minutes per field for an
engineer familiar with the site — stated as an assumption, with no evidence behind it.

### Detection, across all four seeds

| | |
|---|---|
| genuinely degraded cases detected | **122 / 122** (1.00) |
| silent failures caught (≥90% fill, <90% F1) | **26 / 26** (1.00) |
| false alarms on clean + content-churned pages | **0 / 48** — at warn level too |

The false-alarm rate is a **build failure**, not a metric: `make perceive` exits non-zero on any
false alarm or missed degradation. A monitor that cries wolf is worthless.

### The ranker alone, with no model at all

| | |
|---|---|
| top-1: a fully-recovering locator is the ranker's first choice | **0.80 – 0.81** |
| top-3 | 0.82 – 0.83 |

### Ablations — including the two that did not flatter us

`make ablations`. Every arm is deterministic and offline.

| arm | recovery | cases needing repair | model calls |
|---|---|---|---|
| full | **0.87** | 30 | 21 |
| −memory | 0.87 | 30 | 65 |
| −memory (cross-site recall only) | 0.87 | 30 | 27 |
| −regression | 0.87 | 30 | 29 |
| −diff | 0.87 | 30 | 21 |
| −stack | 0.85 | **40** | 24 |
| **−known-good** | **0.13** | 30 | 322 |

**−regression is a null result.** Removing old-page compatibility from both the ranking term and
the G2 gate produces **zero** overfits. The reason is structural: candidates are ranked on
reproducing *known-good* values, and those values came from the pre-break page. G2 rejected a real
overfit during development, and it catches 34 in the B1 baseline — but on our own ranker it is
currently not earning its keep, and we do not claim otherwise.

**−diff is also null for recall,** and the mechanism matters: with classification off, every
episode keys on `UNKNOWN`, so that fingerprint component matches *trivially* rather than being
lost. What it shows is that the fired-signal set alone suffices to key episodes here.

### The challenging case

`content_deferred` on `books`: the page's visible text is removed and `books` ships no structured
data, so the values genuinely are not in the DOM. **Autoheal quarantines rather than guessing.**
That is the correct answer, and it is what the remaining 13% of non-recovered cases are.
[Full trajectory →](trajectories/03-quarantine-human-checkpoint.md)

---

## 6. Improvement Changelog

Each row is a real iteration with the evidence that drove the next decision. Evidence is the same
eval throughout: recovery on degraded cases, scored against frozen ground truth.

| stage | what we tried and why | evidence | decision / learning |
|---|---|---|---|
| **Baseline (B0)** | Static extractor, no healing. Establish the floor before writing any agent code. | recovery **0.00**, mean F1 0.65 over 61 live cases, 6 silent failures | Kept as the floor. The eval existed before the agent, on purpose. |
| **Iteration 1 — perceive** | Nine health signals from runtime provenance, because a broken scraper does not raise. | detection 30/30, **0 false alarms** | Kept. |
| **Iteration 2 — a tenth signal** | First calibration scored our own flagship decoy case at **0.42 (warn)**. A decoy makes the locator match *two* nodes where it matched one, and the runtime already knew. | decoy → **0.85–0.91 critical** | Kept. Added `match_count`. The eval told us the monitor was weak on the case we lead with. |
| **Iteration 3 — localize + rank** | Turn known-good values into candidates, execute each against every record. | ranker top-1 **0.75** | Kept — this is the load-bearing component. |
| **Iteration 4 — bug found by eval** | Every *numeric* field silently produced zero candidates: `money` returns floats, so `37732000.0` normalised to `"377320000"` and never matched page text. | recovery **0.50 → 0.75** | Fixed. No exception anywhere — the project's own failure mode, in our code. |
| **Iteration 5 — exclusion selectors** | G2 kept rejecting the ranker's decoy repair (a positional path: perfect today, wrong on the old page). Correct rejection, fixable cause. | recovery **0.75 → 0.80** | Kept. Generate `span.text:not(.compare-at)` — the fix a human writes. |
| **Iteration 6 — G3 waiver** | G3 could never clear on a patched field: tier/multiplicity baselines describe a locator that no longer exists. | recovery **0.80 → 0.85** | Kept, scoped to patched fields only. An untouched field with a tier shift still blocks. |
| **Iteration 7 — memory transfer** | Recall keyed on concrete locator kind made it a same-site cache. Re-keyed on *strategy class*. | cross-site saving **12% → 19%** | Kept. |
| **Iteration 8 — ablations** | −memory, −regression, −diff, −stack, −known-good. | −known-good **0.87 → 0.13**; −regression and −diff **null** | Kept all, **including the two nulls**. Published as nulls. |
| **Iteration 9 — B1 baseline** | The comparison the brief asks for. First run scored B1 at **0.10**. | on inspection: 5 cases at F1 1.00 failing only G3 | **Our bug.** B1 was never offered the record selector. Corrected to 0.55, then 0.60 after retrying transport errors. |
| **Iteration 10 — hardening** | Crash sweep: 540 mutated pages + 12 adversarial inputs × 7 entry points. | 4 bugs, incl. **every entry point crashing on an empty page** | Fixed. An empty page now reads as zero records and quarantines. |
| **Iteration 11 — corpus 4 → 6 sites** | Power analysis said the result was underpowered. Added a site with **no** structured data and one with a date field. | first result got **worse**: p 0.0625 → 0.18 | B1 won 2 cases the small sample hid. Investigated rather than reverted. |
| **Iteration 12 — the two bugs those cases exposed** | (a) JSON-LD record roots crashed every DOM locator. (b) Candidate matching compared strings, so known-good `91` never matched `"91 points"`. | recovery **0.80 → 0.87**, **p = 0.0156** | Kept. The corpus earned its place by finding bugs, not by adding sample size. |
| **Final** | Everything above, six sites, 375 tests. | **0.87 vs 0.63 vs 0.00**, 0 tokens, 0 overfits | Main contribution: **known-good values as supervision** (§4.1). |

### Experiments we removed or retired

- **"Cycles-to-recover trends down."** Planned as a headline chart. Cycles are **flat at 1.00** —
  every case heals on the first cycle or runs to the cap. The claim was retired, not massaged.
- **A hardcoded conclusion in the ablation output.** The eval *printed* "transfer is the smaller
  half." True at four sites (19% vs 49%), false at six (58% vs 68%), and nothing went red. It is
  now derived from the measurement. **A conclusion baked into a print statement is a claim nothing
  can falsify.**
- **`temperature=0`** was specified in the original plan for determinism. It is removed on Claude
  Opus 5 and returns a 400. Reproducibility comes from the deterministic ranker instead.

---

## 7. Agent trajectories

Representative end-to-end runs for **every agent used**, captured live — prompts and replies are
verbatim, not reconstructions. Regenerate with `make trajectories`.

| trajectory | agent | shows |
|---|---|---|
| [01 — silent failure, healed](trajectories/01-silent-failure-healed.md) | Autoheal repair loop | detection, ranked candidates, three gates, additive patch |
| [02 — memory transfers across sites](trajectories/02-memory-transfers-across-sites.md) | Autoheal repair loop | recall from another site resolving a decision for **0 tokens** |
| [03 — quarantine](trajectories/03-quarantine-human-checkpoint.md) | Autoheal repair loop | an honest refusal and the **human checkpoint** |
| [04 — baseline B1](trajectories/04-baseline-b1-one-shot.md) | One-shot baseline agent | the same repair from raw HTML, for comparison |

---

### Video

`demo/autoheal-demo.mp4` — 4:05, narrated, within the brief's five-minute cap. Built by
`make video` + `make video-assemble`, which renders title cards and captured dashboard states
against a generated narration track. It opens on the problem, walks one real repair end to end,
and closes on the comparison and the changelog.

**Disclosure: the narration is synthesised speech** (macOS `say`), not a human recording. The
build refuses to run if any spoken number disagrees with `demo/events.json` — the token count is
read straight out of the recording rather than typed into the script, because a hardcoded figure
went stale the first time the demo was re-recorded and the check caught it.

## 8. Reproduction guide

Written for someone starting from a clean environment.

### Requirements

| | |
|---|---|
| Python | 3.13 (3.13.2 used) |
| Package manager | [`uv`](https://docs.astral.sh/uv/) 0.10.7 |
| Runtime deps | `lxml` 6.1.2, `pydantic` 2.13.5, `cssselect` 1.5.0 |
| Network | **none required** for any result in §5 |
| API key | **none required** |
| Disk | ~40 MB |
| OS | developed on macOS; pure Python plus `lxml`, so Linux and Windows work. `make` and `open` are the only macOS/Linux-isms — see the no-`make` commands in §3 |

### Data

All six sites are **frozen in the repo** under `eval/sites/`. `books` and `quotes` are pages from
[toscrape.com](https://toscrape.com), a sandbox published expressly for scraper testing.
`wikitable` is Wikipedia markup (CC BY-SA). `shop`, `hn` and `jobs` are **synthetic**, generated by
committed seeded scripts. No private data, no credentials, nothing fetched at eval time.

### Commands

```bash
uv sync                                # install, ~10s

make verify        # 1s    independent ground-truth checks (not the tautological F1 test)
make test          # 66s   375 tests
make eval          # <1s   B0 static baseline  -> results/seed{N}/b0_static.json
make check         # 1s    fails if the committed B0 numbers no longer reproduce
make perceive      # 1s    detection vs false alarms; NON-ZERO EXIT on either
make heal          # 24s   recovery, ranker accuracy, memory ablation
make check-heal    # 24s   fails if the committed healing numbers no longer reproduce
make ablations     # ~4m   which components are load-bearing, including nulls
make all           # ~2m   verify + test + check + perceive + heal + check-heal
```

### Expected output

`make all` ends with two lockfile confirmations. The headline numbers:

```
RECOVERY             : 0.87   (B0 static baseline: 0.00 by construction)
mean F1              : 0.289 -> 0.914
detection rate       : 1.00  (30/30)
FALSE ALARM (fire)   : 0.00  (0/12)
healthy-case damage  : 0
```

Any deviation fails `make check` / `make check-heal` rather than passing quietly.

### Optional: the parts that need a model

Not required to reproduce anything in §5. Both arms used a **free** model.

```bash
ollama pull gpt-oss:120b-cloud                     # or any tool-calling model
export AUTOHEAL_LLM=ollama:gpt-oss:120b-cloud      # also accepts anthropic[:model]

make b1              # ~10m   B1 one-shot baseline  (NOT reproducible: a model decides it)
make trajectories    # ~2m    regenerate the four agent trajectories
make demo            # ~40s   re-record the demo
```

B1 is explicitly **not reproducible** and it swings: runs of the identical configuration gave
0.55 / 0.60 / 0.63 recovery. It is excluded from the drift lockfiles for that reason. Autoheal's
column is byte-identical across runs and across three `PYTHONHASHSEED` values.

### Verifying reproducibility yourself

```bash
for hs in 0 1 12345; do PYTHONHASHSEED=$hs uv run python -m eval.heal_eval --seed 0 \
  --out /tmp/hs$hs >/dev/null; done
diff /tmp/hs0/seed0/heal.json /tmp/hs12345/seed0/heal.json && echo identical
```

CI runs exactly this on every push for `harness`, `heal_eval` and `perceive_eval`. `ablations`
is verified the same way but locally: it is seven calls to the same `run_matrix` that `heal_eval`
already drives under all three seeds, so re-checking it in CI proved nothing new and cost about
four minutes per seed.

---

## 9. Main failure mode

**Content that is genuinely gone.** When a page moves to client-side rendering and ships no
structured data, the values are not in the DOM and no locator can find them. Autoheal detects
this correctly and **quarantines**: extraction pauses, a human-review card is emitted, and nothing
is written. All non-recovered cases in §5 are this.

That is the designed behaviour, but it is worth naming as a limit: **Autoheal cannot recover data
that is not present.** It can only tell you, immediately and loudly, that it is not present —
which is still a large improvement on finding out in three weeks.

Two smaller limits, stated plainly:

- **The model contributes nothing measurable** on this corpus — see §4.7, where that is stated
  plainly rather than buried. It is not the result we set out to get, and it is the one a judge
  should hear from us first.
- **Six sites and synthetic mutations.** The mutators never see the spec and are site-agnostic,
  but they are still a breakage distribution we authored. No real redesign has been tested.

---

## 10. Hot take

> **The hardest bug in an agent pipeline is the one where every component reports success.**

Every genuine bug in this project lived in a path that nothing had ever *executed*, and each one
produced a plausible wrong answer rather than an error:

- A `money` transform returned floats, so `37732000.0` never matched the text `37,732,000`. Every
  numeric field silently generated **zero** repair candidates. No exception.
- `t_iso_date("2024-13-45")` returned it verbatim — month 13, day 45 — as a valid-looking date.
- The demo page embedded JSON containing `</script>`, which closed the host tag early. The file
  was written, the byte count was right, and the page was dead.
- The B1 harness scored the baseline at **0.10** because we never gave it the record selector.
- The ablation output *printed* a conclusion that had become false.

**The practical lesson: for every component, ask "what does this look like when it fails
silently?" and build the detector before the feature.** Concretely, three things earned their
keep more than any prompt engineering did:

1. **A negative control.** `content_churn` — new content, unchanged structure — is why the
   false-alarm rate means something. Without it the FPR is measured against a byte-identical page
   and is trivially zero.
2. **Checkers that are proven able to fail.** Both drift lockfiles have tests that corrupt the
   committed numbers and assert a non-zero exit. A check that has only ever passed is not evidence.
3. **Invariance tests over value tests.** The `−known-good` ablation leaked three separate ways
   until we asserted that corrupting the known-good values must not change the ranking *at all*.
   Pairwise score comparisons passed while the leaks were live.

What we would build differently next time: **write the eval and its negative control first, then
the feature.** We did this for extraction and it caught four defects; we did not do it for the
demo recorder and shipped a page that was broken on open.

---

## 11. What existed before, and what was built here

**Pre-existing, used under their licences:** Python 3.13, `lxml`, `cssselect`, `pydantic`, `uv`,
`pytest`; the Ollama runtime and the open-weight `gpt-oss` model; `toscrape.com` (a public
scraping sandbox) and Wikipedia markup (CC BY-SA) as frozen corpus pages.

**Built for this hackathon — all of it:** every module in `autoheal/`, every harness in `eval/`,
the mutators, the six-site corpus and its ground truth, all 375 tests, the demo recorder and
player, the trajectories, and this README. The repository contained no prior work; the first
commit is from the start of the event.

**Ground-rules compliance.** No credentials or private data are in the repository, and none are
required. All corpus data is public or synthetic. No consequential action is taken without a human
checkpoint: an unverifiable repair quarantines and hands over. Every number in §5 links to a
committed artefact under `results/`, regenerable with the commands in §8.

---

## Appendix A — the eval is built not to flatter us

- **Mutators never see the spec.** They locate the repeating record container heuristically. We are
  not breaking exactly what we know how to fix.
- **Decoy markers are drawn per seed** from a pool of real "compare at" class names. No marker
  string appears anywhere in `autoheal/`, and two tests assert both halves: that the repair adapts
  to whichever marker a seed chose, and that the repair code names none of them.
- **`jsonld_drop` exists to defeat our own fallbacks.** Without it, pages with structured data
  survive almost any redesign via tier 2 and the repair loop is never exercised.
- **Ground truth is independently verified.** `truth.json` is v1's own output, so scoring v1
  against it is a tautology. `make verify` checks it by a *different* mechanism — raw-text regex
  over the HTML and domain invariants (the wikitable is a ranked list, so population must be
  strictly descending). `verify_truth` refuses to pass a site that has only generic checks.
- **`hn` is the hostile case:** no structured data at all, values embedded in prose (`91 points`),
  and nav chrome reusing the record classes.

## Appendix B — corpus

| site | source | records |
|---|---|---|
| `books` | books.toscrape.com (scraping sandbox), frozen | 20 |
| `quotes` | quotes.toscrape.com (scraping sandbox), frozen | 10 |
| `wikitable` | real `wikitable` markup from Wikipedia | 39 |
| `shop` | synthetic SSR product grid with JSON-LD | 24 |
| `hn` | synthetic link-aggregator listing, **no structured data** | 30 |
| `jobs` | synthetic job board: JSON-LD + microdata + definition list | 18 |

Eight seeded, composable mutations at three severities, plus `content_churn` as a negative
control. `decoy_injection` is the flagship: it clones a field node, gives it a plausible wrong
value, and inserts it ahead of the real one. Nothing errors; fill rate stays 100%.

## Appendix C — repository layout

```
autoheal/      spec · runtime · perceive · diff · localize · patch · verify · memory · diagnose · loop
eval/          sites/ (6, frozen + their generators) · mutators · harness
               perceive_eval · heal_eval · ablations · b1_oneshot · trajectories
               verify_truth · check_baseline · check_heal · author_specs · freeze_truth
demo/          record.py → events.json → replay.html   (self-contained, offline)
               cards.html + make_video.py → autoheal-demo.mp4
trajectories/  four captured agent runs, prompts and replies verbatim
results/       committed numbers for seeds 0–3; the drift lockfiles compare against these
tests/         375 tests across six files
PLAN.md        the pre-build plan, unedited — what the changelog measures against
```

Ten of the eleven `autoheal/` modules never call a model. That is the engineering point: the agent
is the small, constrained part of a mostly deterministic system.
