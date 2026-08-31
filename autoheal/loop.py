"""The closed loop: perceive -> diagnose -> patch -> verify -> remember.

Every cycle is a full round trip through the gates. A patch that fails is not
discarded silently -- the strategy that lost is written to memory and excluded
from the next cycle, which is what stops the loop proposing the same broken idea
four times and calling it four attempts.

After the cap the site is quarantined and a human-review card is emitted. That is
a feature, not a shortfall: the premise of the project is that a confident wrong
answer is worse than an admission of defeat, and the loop has to honour that when
it is the one that cannot fix it.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .diagnose import Diagnosis, diagnose
from .diff import DomDiff, diff
from .localize import induce_root_candidates, strategy_of
from .memory import Episode, Fingerprint, Store
from .metrics import score
from .patch import SpecPatch, apply_patch, record_hits, spec_diff
from .perceive import RECORD, Baseline, BreakageReport, perceive
from .runtime import Context, extract
from .spec import ExtractorSpec
from .verify import TAU, Verdict, verify

MAX_CYCLES = 4
MAX_FIELDS_PER_CYCLE = 6


class HealResult(BaseModel):
    site: str
    healed: bool = False
    quarantined: bool = False
    fired: bool = False
    cycles: int = 0
    tokens: int = 0
    used_memory: bool = False
    used_llm: bool = False
    llm_calls: int = 0        # model calls the loop actually needed
    llm_calls_avoided: int = 0  # ambiguous decisions memory resolved for free
    diff_class: str = "UNKNOWN"
    from_version: int = 1
    to_version: int = 1
    f1_before: float = 0.0
    f1_after: float = 0.0
    fields_repaired: list[str] = Field(default_factory=list)
    patches: list[str] = Field(default_factory=list)
    spec_diff: list[str] = Field(default_factory=list)
    gates: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    card: str | None = None
    spec: ExtractorSpec | None = None
    # Per-field diagnosis detail, in cycle order: the ranked candidates that were
    # considered, what memory recalled, and why the winner won. `HealResult` used
    # to flatten all of this away, which left the agent trace with nothing to show
    # between "the DOM changed" and "here is the patch".
    diagnoses: list[Diagnosis] = Field(default_factory=list)

    def summary(self) -> str:
        if not self.fired:
            return "healthy: no breakage signal, nothing to repair"
        state = "HEALED" if self.healed else ("QUARANTINED" if self.quarantined else "no-op")
        return (f"{state} {self.site} v{self.from_version}->v{self.to_version}"
                f" in {self.cycles} cycle(s), {self.tokens} tokens,"
                f" F1 {self.f1_before:.2f} -> {self.f1_after:.2f}")


def heal(
    spec: ExtractorSpec,
    *,
    broken_html: str,
    good_html: str,
    known_good: list[dict],
    baseline: Baseline,
    store: Store | None = None,
    base_url: str = "",
    use_llm: bool = False,
    max_cycles: int = MAX_CYCLES,
    tau: float = TAU,
    run_id: int = 1,
    cross_site_only: bool = False,
    gates: bool = True,
    use_diff: bool = True,
    regression_aware: bool = True,
    known_good_aware: bool = True,
) -> HealResult:
    """Repair `spec` against a page that broke. `known_good` is the last output
    we trust -- never the eval's frozen truth, which the loop must not see."""
    names = spec.field_names()
    res = HealResult(site=spec.site, from_version=spec.version, to_version=spec.version, spec=spec)

    run0 = extract(spec, broken_html, base_url=base_url, run_id=run_id)
    before = perceive(run0, baseline, spec)
    res.fired = before.fired
    res.f1_before = round(score(run0.values(), known_good, names).macro_f1, 4)
    res.evidence = before.evidence()[:8]
    if not before.fired:
        return res

    # `use_diff=False` is the -structural-diff ablation: the loop still repairs,
    # but with no idea what changed, so episodes key on UNKNOWN and recall blurs.
    d = diff(good_html, broken_html) if use_diff else DomDiff()
    res.diff_class = d.primary

    current = spec
    avoid: dict[str, set[str]] = {}

    for cycle in range(1, max_cycles + 1):
        res.cycles = cycle
        run = extract(current, broken_html, base_url=base_url, run_id=run_id)
        report = perceive(run, baseline, current)
        if not report.fired and cycle > 1:
            break

        patches: list[SpecPatch] = []
        diags: list[Diagnosis] = []

        # The record selector first: there is nothing to localize field values
        # *inside* until the page yields records again -- and a root that merely
        # fell through to a fallback tier still has to be promoted, or G3 can
        # never clear no matter how well every field is repaired.
        root_sick = RECORD in report.fields and report.fields[RECORD].severity == "critical"
        staged = current
        if not run.records or (root_sick and run.root_tier not in (0, None)):
            root = _repair_roots(current, run, broken_html, known_good, base_url)
            if root is None and not run.records:
                return _quarantine(res, current, "no record selector could be induced on the broken page")
            if root is not None:
                patches.append(root)
                # Stage it immediately. Field candidates are generated *inside*
                # record roots, so diagnosing fields against the unpatched spec
                # on a page whose root selector is dead finds nothing at all --
                # every cycle then re-proposes the same root fix and quarantines
                # with the fields never once looked at.
                staged = apply_patch(current, [root], created_by="autoheal", note="staged root repair")
                run = extract(staged, broken_html, base_url=base_url, run_id=run_id)
                report = perceive(run, baseline, staged)

        for field in report.broken[:MAX_FIELDS_PER_CYCLE]:
            if field not in staged.fields:
                continue
            dx = diagnose(
                staged, report, field,
                broken_html=broken_html, good_html=good_html, known_good=known_good,
                store=store, dom_diff=d, base_url=base_url, use_llm=use_llm,
                avoid=avoid.get(field, set()), cross_site_only=cross_site_only,
                regression_aware=regression_aware, known_good_aware=known_good_aware,
            )
            diags.append(dx)
            res.diagnoses.append(dx)
            res.tokens += dx.tokens
            res.used_memory |= dx.used_memory
            res.used_llm |= dx.used_llm
            res.llm_calls += bool(dx.would_call_llm)
            res.llm_calls_avoided += bool(dx.resolved_ambiguity)
            if dx.patch is not None:
                patches.append(dx.patch)

        if not patches:
            return _quarantine(res, current, "no candidate locator recovered any known-good value")

        note = f"cycle {cycle}: {d.summary()} -> " + "; ".join(p.describe() for p in patches[:3])
        # Patches are applied to `current`, not to `staged`: the staged root fix
        # is in `patches` too, so this yields exactly one new version per cycle.
        cand_spec = apply_patch(current, patches, created_by="autoheal", note=note)
        v = verify(cand_spec, broken_html=broken_html, good_html=good_html, known_good=known_good,
                   baseline=baseline, before=before, base_url=base_url, tau=tau,
                   patched_fields={p.field for p in patches if p.field})
        res.gates = [f"{g.name}={'pass' if g.passed else 'FAIL'}" for g in v.gates]

        # `gates=False` is the -verify ablation: take the first patch the ranker
        # proposes. The verdict is still computed so the report can show what was
        # waved through, which is the entire point of running the arm.
        if v.passed or (not gates and _gates_except_regression(v)):
            healed_run = extract(cand_spec, broken_html, base_url=base_url, run_id=run_id)
            final = record_hits(cand_spec, healed_run)
            res.healed = True
            res.to_version = final.version
            res.f1_after = v.f1_broken
            res.fields_repaired = sorted({p.field for p in patches if p.field})
            res.patches = [p.describe() for p in patches]
            res.spec_diff = spec_diff(spec, final)
            res.spec = final
            _remember(store, diags, current, res, v, outcome="healed", cross_site_only=cross_site_only)
            if store is not None:
                store.save_spec(final)
                store.save_records(healed_run)
                store.save_snapshot(spec.site, run_id, broken_html)
                store.mark_good(spec.site, run_id)
                store.save_baseline(baseline.fold(healed_run))
            return res

        # Failed: remember what lost, exclude it, and try again with what is left.
        res.f1_after = v.f1_broken
        for dx in diags:
            if dx.patch is not None:
                avoid.setdefault(dx.field, set()).add(dx.patch.strategy)
        _remember(store, diags, current, res, v, outcome="failed", cross_site_only=cross_site_only)
        res.evidence += [f"cycle {cycle} rejected: {f}" for f in v.failures()]

    return _quarantine(res, current, "; ".join(res.gates) or "gates never passed")


def _gates_except_regression(v: Verdict) -> bool:
    """With the gates ablated we still require the loop to believe it succeeded;
    otherwise the arm measures nothing but the cap. Only G2 is waived."""
    return all(g.passed for g in v.gates if g.name != "G2-regression")


def _repair_roots(spec: ExtractorSpec, run, broken_html: str, known_good: list[dict], base_url: str) -> SpecPatch | None:
    expected = len(known_good)
    # A fallback tier that is already finding the *right* records is the best
    # available answer: promote it rather than inventing a new selector. But only
    # if the count is right -- a fallback that sweeps in two header rows looks
    # like a working selector and silently caps recovery below every gate.
    if run.records and run.root_tier not in (0, None) and abs(run.n_roots - expected) <= 0.02 * max(expected, 1):
        loc = spec.record_selector[run.root_tier]
        return SpecPatch(field=None, locator=loc, strategy=f"promote_root_tier:{loc.kind}",
                         reason=f"record selector fell through to tier {run.root_tier};"
                                f" {loc.q!r} still finds all {run.n_roots} records")
    ctx = Context.build(broken_html, base_url)
    vals = [v for r in known_good for v in r.values() if v is not None][:60]
    cands = induce_root_candidates(ctx.doc, expected_n=expected or None, values=vals)
    if not cands:
        return None
    best = cands[0]
    if run.records and best.n_hits_mean and abs(best.n_hits_mean - expected) > abs(run.n_roots - expected):
        return None  # induction is no better than what we already have
    return SpecPatch(field=None, locator=best.locator, strategy="induce_roots",
                     reason=f"record selector matched nothing; induced {best.locator.q!r}"
                            f" ({int(best.n_hits_mean)} repeating siblings)")


def _remember(store, diags: list[Diagnosis], spec, res: HealResult, v: Verdict, *, outcome: str, cross_site_only: bool) -> None:
    if store is None:
        return
    for dx in diags:
        if dx.patch is None:
            continue
        store.append_episode(Episode(
            site=spec.site, field=dx.field, spec_version=spec.version, fingerprint=dx.fingerprint,
            strategy=dx.patch.strategy, strategy_class=strategy_of(dx.patch.locator),
            locator=dx.patch.locator, outcome=outcome,
            cycles=res.cycles, tokens=dx.tokens, used_memory=dx.used_memory,
            f1_before=res.f1_before, f1_after=v.f1_broken,
            gates={g.name: g.passed for g in v.gates}, note=dx.rationale[:300],
        ))


def _quarantine(res: HealResult, spec: ExtractorSpec, why: str) -> HealResult:
    """A clean 'I could not fix this, and here is exactly what I tried'."""
    res.quarantined = True
    res.card = "\n".join([
        f"QUARANTINE  {spec.site}  spec v{spec.version}",
        f"  structural change : {res.diff_class}",
        f"  cycles spent      : {res.cycles} (cap {MAX_CYCLES})",
        f"  last gate state   : {', '.join(res.gates) or 'never reached verification'}",
        f"  reason            : {why}",
        "  monitor evidence  :",
        *[f"    - {e}" for e in res.evidence[:6]],
        "  action            : extraction is PAUSED for this site. No records will be",
        "                      written until a human reviews the spec. This is deliberate:",
        "                      stale-but-flagged beats fresh-and-wrong.",
    ])
    return res
