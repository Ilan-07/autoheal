"""PERCEIVE: nine health signals over a run's provenance. No LLM, no exceptions.

The premise of the project is that a broken scraper does not raise. It returns
records. So nothing here waits for an error -- every signal compares *this* run
against a rolling baseline of healthy runs and reports a deviation.

Two design rules earned the hard way in stage 1:

* **Everything is judged against a baseline, never against an absolute.** A field
  that has always been constant is not evidence (every book on the `books` page
  really is "In stock"); a field that *newly* collapses to one value is.
* **A signal must be able to fire while the values still look fine.** Signal 2,
  the locator-tier shift, is the sharpest one we have precisely because it fires
  on a *successful* extraction that quietly changed how it succeeded.

`PLAN.md` specified nine signals. There are ten. The tenth, `match_count`, was
added after the first calibration run scored our own flagship case -- an injected
decoy price -- at 0.42, a warn, on drift and novelty alone. A decoy does not just
change the value; it makes the locator match *two* nodes where it matched one,
and the runtime already knew that and was throwing it away. Adding the signal is
recorded here rather than quietly folded into signal 2, because "the eval said
the monitor was weak" is the reason it exists.
"""

from __future__ import annotations

import bisect
import math
import re
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, Field

from .spec import ExtractionRun, ExtractorSpec

Severity = Literal["ok", "warn", "critical"]

# Per-signal weight in the noisy-or health score. These are the knobs that trade
# detection rate against false-alarm rate; both are reported by the eval, so a
# change here is visible as a number rather than as a vibe.
WEIGHTS: dict[str, float] = {
    "fill_drop": 1.00,       # 1. the selector stopped matching
    "tier_shift": 0.85,      # 2. a fallback is quietly carrying the field
    "record_count": 1.00,    # 3. the record set itself changed size
    "validator_fail": 0.80,  # 4. values arrive but no longer typecheck
    "value_drift": 0.55,     # 5. right shape, wrong node or wrong units
    "constant_collapse": 0.90,  # 6. selector now hits shared chrome
    "invariant": 1.00,       # 7. plausible garbage that violates a known rule
    # 8 is weighted below WARN on purpose: novelty *alone* is a site publishing
    # new content, which is not a breakage. It only matters alongside signal 2.
    "novel_values": 0.25,
    "shape_entropy": 0.50,   # 9. partial breakage across records
    "match_count": 0.85,     # 10. the locator matches more nodes than it used to
}

WARN, CRITICAL = 0.30, 0.60
RECORD = "<record>"  # pseudo-field for site-level signals

_SAMPLE_CAP = 400
_WORD = re.compile(r"[a-z0-9]+")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# --- small statistics -----------------------------------------------------


def _clamp(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return 0.0 if not n else (s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2)


def _iqr(xs: list[float]) -> float:
    s = sorted(xs)
    if len(s) < 4:
        return 0.0
    return s[int(0.75 * (len(s) - 1))] - s[int(0.25 * (len(s) - 1))]


def _ks(a: list[float], b: list[float]) -> float:
    """Two-sample Kolmogorov-Smirnov statistic: max ECDF gap, 0..1."""
    if not a or not b:
        return 0.0
    sa, sb = sorted(a), sorted(b)
    return max(
        abs(bisect.bisect_right(sa, x) / len(sa) - bisect.bisect_right(sb, x) / len(sb))
        for x in set(sa) | set(sb)
    )


def _tvd(a: Counter, b: Counter) -> float:
    """Total-variation distance between two categorical distributions."""
    na, nb = sum(a.values()), sum(b.values())
    if not na or not nb:
        return 0.0
    return 0.5 * sum(abs(a[k] / na - b[k] / nb) for k in set(a) | set(b))


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a or b) else 1.0


def _entropy(counts: Counter) -> float:
    n = sum(counts.values())
    if n <= 1:
        return 0.0
    return -sum((c / n) * math.log2(c / n) for c in counts.values() if c)


def _tokens(values: list[Any]) -> set[str]:
    out: set[str] = set()
    for v in values:
        out.update(_WORD.findall(str(v).lower()))
    return out


def _norm(v: Any) -> Any:
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        return " ".join(v.split())
    return v


def _numbers(values: list[Any]) -> list[float]:
    return [float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]


# --- baseline -------------------------------------------------------------


class FieldBaseline(BaseModel):
    """What a healthy run of this field looks like."""

    fill: float = 0.0
    modal_tier: int | None = None
    tier_share: dict[str, float] = Field(default_factory=dict)
    validator_pass: float = 1.0
    distinct_rate: float = 1.0
    numeric: bool = False
    median: float | None = None
    iqr: float | None = None
    len_mean: float | None = None
    all_positive: bool = False
    match_mean: float = 1.0  # mean nodes matched per record by the winning locator
    # A capped sample of observed values. One list serves KS, token-Jaccard, TVD
    # and the novelty check, so the baseline stays small enough to eyeball.
    sample: list[Any] = Field(default_factory=list)

    @classmethod
    def observe(cls, values: list[Any], tiers: list[int | None], n_records: int,
                hits: list[int] | None = None) -> FieldBaseline:
        present = [_norm(v) for v in values if v is not None]
        nums = _numbers(present)
        hit_tiers = [t for t in tiers if t is not None]
        share = Counter(str(t) for t in hit_tiers)
        return cls(
            fill=len(present) / n_records if n_records else 0.0,
            modal_tier=min(Counter(hit_tiers), key=lambda t: (-Counter(hit_tiers)[t], t)) if hit_tiers else None,
            tier_share={k: v / len(hit_tiers) for k, v in share.items()} if hit_tiers else {},
            validator_pass=len(present) / n_records if n_records else 0.0,
            distinct_rate=len(set(map(str, present))) / len(present) if present else 1.0,
            numeric=bool(nums) and len(nums) == len(present),
            median=_median(nums) if nums else None,
            iqr=_iqr(nums) if nums else None,
            len_mean=(sum(len(str(v)) for v in present) / len(present)) if present else None,
            all_positive=bool(nums) and all(n > 0 for n in nums),
            match_mean=(sum(h for h in hits if h) / max(sum(1 for h in hits if h), 1)) if hits else 1.0,
            sample=present[:_SAMPLE_CAP],
        )


class Baseline(BaseModel):
    """Rolling per-site stats. `runs` is the number of healthy runs folded in."""

    site: str
    spec_version: int = 1
    runs: int = 0
    n_records: float = 0.0
    root_tier: int | None = None
    shape_entropy: float = 0.0
    fields: dict[str, FieldBaseline] = Field(default_factory=dict)

    @classmethod
    def observe(cls, run: ExtractionRun) -> Baseline:
        n = len(run.records)
        b = cls(site=run.site, spec_version=run.spec_version, runs=1, n_records=float(n),
                root_tier=run.root_tier, shape_entropy=_shape_entropy(run))
        for name in _field_names(run):
            b.fields[name] = FieldBaseline.observe(
                [r.fields[name].value for r in run.records],
                [r.fields[name].tier for r in run.records],
                n,
                [r.fields[name].n_hits for r in run.records],
            )
        return b

    def fold(self, run: ExtractionRun, *, alpha: float = 0.3) -> Baseline:
        """Fold one more *known-good* run in. Scalars move by EWMA so a slow
        legitimate drift is absorbed; samples accumulate so novelty stays honest."""
        fresh = Baseline.observe(run)
        out = self.model_copy(deep=True)
        out.runs = self.runs + 1
        out.spec_version = run.spec_version
        out.n_records = (1 - alpha) * self.n_records + alpha * fresh.n_records
        out.shape_entropy = (1 - alpha) * self.shape_entropy + alpha * fresh.shape_entropy
        out.root_tier = fresh.root_tier
        for name, nb in fresh.fields.items():
            ob = out.fields.get(name)
            if ob is None:
                out.fields[name] = nb
                continue
            merged = nb.model_copy(deep=True)
            for attr in ("fill", "validator_pass", "distinct_rate", "match_mean"):
                merged.__dict__[attr] = (1 - alpha) * getattr(ob, attr) + alpha * getattr(nb, attr)
            merged.all_positive = ob.all_positive and nb.all_positive
            seen = list(dict.fromkeys(map(_key, ob.sample)))
            merged.sample = (ob.sample + [v for v in nb.sample if _key(v) not in set(seen)])[:_SAMPLE_CAP]
            out.fields[name] = merged
        return out


def _key(v: Any) -> str:
    return str(_norm(v))


def _field_names(run: ExtractionRun) -> list[str]:
    return list(run.records[0].fields) if run.records else []


def _shape_entropy(run: ExtractionRun) -> float:
    """Entropy over which fields are present per record. A page where every
    record carries the same fields scores 0; partial breakage raises it."""
    pat = Counter(
        tuple(sorted(n for n, f in r.fields.items() if f.value is not None)) for r in run.records
    )
    return _entropy(pat)


# --- report ---------------------------------------------------------------


class Signal(BaseModel):
    name: str
    field: str
    magnitude: float  # 0..1, how far past the threshold this went
    evidence: str  # human-readable; goes verbatim into the diagnose prompt

    @property
    def weight(self) -> float:
        return WEIGHTS[self.name]


class FieldHealth(BaseModel):
    field: str
    score: float = 0.0
    severity: Severity = "ok"
    signals: list[Signal] = Field(default_factory=list)


class BreakageReport(BaseModel):
    site: str
    run_id: int = 0
    spec_version: int = 1
    fields: dict[str, FieldHealth] = Field(default_factory=dict)

    @property
    def fired(self) -> bool:
        return any(h.severity == "critical" for h in self.fields.values())

    @property
    def broken(self) -> list[str]:
        """Fields the repair loop should work on, worst first."""
        bad = [h for h in self.fields.values() if h.severity != "ok" and h.field != RECORD]
        return [h.field for h in sorted(bad, key=lambda h: (-h.score, h.field))]

    @property
    def signal_names(self) -> list[str]:
        return sorted({s.name for h in self.fields.values() for s in h.signals})

    def evidence(self, field: str | None = None) -> list[str]:
        hs = self.fields.values() if field is None else [self.fields[field]]
        return [f"[{s.field}] {s.evidence}" for h in hs for s in sorted(h.signals, key=lambda s: -s.magnitude)]

    def summary(self) -> str:
        if not self.fields:
            return "healthy"
        worst = max(self.fields.values(), key=lambda h: h.score)
        return f"{'BREAKAGE' if self.fired else 'ok'} score={worst.score:.2f} fields={self.broken or '-'}"


def rescore(field: str, signals: list[Signal]) -> FieldHealth:
    """Public re-entry to the scorer, for callers that need to drop signals."""
    return _health(field, signals)


def _health(field: str, signals: list[Signal]) -> FieldHealth:
    """Noisy-or over weighted signals: several weak signals agreeing beats one
    loud one, and no single signal can pin the score at 1.0 on its own."""
    acc = 1.0
    for s in signals:
        acc *= 1 - _clamp(s.magnitude) * s.weight
    score = 1 - acc
    sev: Severity = "critical" if score >= CRITICAL else "warn" if score >= WARN else "ok"
    return FieldHealth(field=field, score=round(score, 4), severity=sev,
                       signals=sorted(signals, key=lambda s: -s.magnitude * s.weight))


# --- the nine signals -----------------------------------------------------


def perceive(run: ExtractionRun, baseline: Baseline, spec: ExtractorSpec | None = None) -> BreakageReport:
    """Compare one run against the baseline and report per-field health."""
    rep = BreakageReport(site=run.site, run_id=run.run_id, spec_version=run.spec_version)
    n = len(run.records)

    rep.fields[RECORD] = _health(RECORD, _record_signals(run, baseline, n))

    for name in sorted(set(_field_names(run)) | set(baseline.fields)):
        fb = baseline.fields.get(name)
        if fb is None:
            continue
        values = [r.fields[name].value for r in run.records if name in r.fields]
        tiers = [r.fields[name].tier for r in run.records if name in r.fields]
        fails = [r.fields[name].failed_validator for r in run.records if name in r.fields]
        sigs: list[Signal] = []
        hits = [r.fields[name].n_hits for r in run.records if name in r.fields]
        sigs += _fill_signal(name, values, tiers, fb, n, spec)
        sigs += _match_signal(name, hits, fb)
        sigs += _tier_signal(name, tiers, fb)
        sigs += _validator_signal(name, fails, values, fb, n)
        sigs += _drift_signal(name, values, fb)
        sigs += _collapse_signal(name, values, fb)
        sigs += _invariant_signal(name, values, fb, baseline)
        sigs += _novelty_signal(name, values, fb)
        rep.fields[name] = _health(name, sigs)
    return rep


def _record_signals(run: ExtractionRun, b: Baseline, n: int) -> list[Signal]:
    out: list[Signal] = []
    # 3. record count
    if b.n_records:
        delta = abs(n - b.n_records) / b.n_records
        if delta > 0.05:
            out.append(Signal(name="record_count", field=RECORD, magnitude=_clamp(delta / 0.5),
                              evidence=f"record count {n} vs baseline {b.n_records:.0f} ({delta:+.0%})"))
    elif n:
        out.append(Signal(name="record_count", field=RECORD, magnitude=1.0,
                          evidence=f"record count {n} with an empty baseline"))
    # 2b. the record selector itself fell through to a fallback
    if run.root_tier != b.root_tier:
        was, now = b.root_tier, run.root_tier
        mag = 1.0 if now is None else _clamp(0.5 + 0.25 * abs((now or 0) - (was or 0)))
        out.append(Signal(name="tier_shift", field=RECORD, magnitude=mag,
                          evidence=f"record selector served by tier {now} (baseline tier {was})"))
    # 9. shape entropy
    now_e = _shape_entropy(run)
    if now_e > b.shape_entropy + 0.15:
        out.append(Signal(name="shape_entropy", field=RECORD, magnitude=_clamp((now_e - b.shape_entropy) / 1.0),
                          evidence=f"per-record field-presence entropy {now_e:.2f} vs {b.shape_entropy:.2f}"
                                   " -- records no longer agree on which fields exist"))
    for err in run.errors:
        out.append(Signal(name="invariant", field=RECORD, magnitude=0.6, evidence=f"runtime: {err}"))
    return out


def _fill_signal(name, values, tiers, fb: FieldBaseline, n: int, spec) -> list[Signal]:
    """1. Field fill-rate vs baseline. The blunt one: the selector matches nothing."""
    fill = sum(1 for v in values if v is not None) / n if n else 0.0
    floor = spec.fields[name].min_fill if spec and name in spec.fields else 0.95
    drop = fb.fill - fill
    if drop <= 0.02 and fill >= min(floor, fb.fill):
        return []
    mag = _clamp(max(drop / max(fb.fill, 0.01), (floor - fill) / max(floor, 0.01)))
    return [Signal(name="fill_drop", field=name, magnitude=mag,
                   evidence=f"fill {fill:.0%} vs baseline {fb.fill:.0%} (floor {floor:.0%})")]


def _tier_signal(name, tiers, fb: FieldBaseline) -> list[Signal]:
    """2. Locator-tier shift -- fires on values that still look perfectly fine.

    This is the signal nothing else in the space has, and the only one that
    catches a rename the stack silently absorbed. A *deeper* tier means the
    primary locator stopped working; a shallower one means it came back."""
    hit = [t for t in tiers if t is not None]
    if not hit or fb.modal_tier is None:
        return []
    now = Counter(hit)
    modal = min(now, key=lambda t: (-now[t], t))
    moved = sum(c for t, c in now.items() if t != fb.modal_tier) / len(hit)
    if modal == fb.modal_tier and moved < 0.10:
        return []
    depth = modal - fb.modal_tier
    mag = _clamp(0.45 + 0.2 * depth if depth > 0 else 0.30) * (0.5 + 0.5 * moved)
    return [Signal(name="tier_shift", field=name, magnitude=mag,
                   evidence=f"served by locator tier {modal} (baseline tier {fb.modal_tier});"
                            f" {moved:.0%} of records shifted tier")]


def _match_signal(name, hits, fb: FieldBaseline) -> list[Signal]:
    """10. Match multiplicity. The locator resolves to more nodes than it used to.

    This is the decoy detector. An injected 'compare at' price leaves fill,
    tier, validators and record count all perfectly healthy -- the only thing
    that changed is that `span.product-price` now matches twice per record and
    the runtime silently kept the first one."""
    live = [h for h in hits if h]
    if not live or fb.match_mean <= 0:
        return []
    now = sum(live) / len(live)
    if now <= fb.match_mean + 0.15:
        return []
    extra = now - fb.match_mean
    return [Signal(name="match_count", field=name, magnitude=_clamp(extra / 1.0),
                   evidence=f"locator now matches {now:.2f} nodes per record (baseline"
                            f" {fb.match_mean:.2f}) -- an extra node is shadowing the real one")]


def _validator_signal(name, fails, values, fb: FieldBaseline, n: int) -> list[Signal]:
    """4. Validator pass-rate. A value arrived but no locator produced one that
    typechecked -- format drift, or the wrong node entirely."""
    failed = [f for f in fails if f]
    if not failed:
        return []
    rate = len(failed) / n if n else 0.0
    top = Counter(failed).most_common(1)[0][0]
    return [Signal(name="validator_fail", field=name, magnitude=_clamp(rate / 0.5),
                   evidence=f"{rate:.0%} of records exhausted the stack on a failing"
                            f" '{top}' validator")]


def _drift_signal(name, values, fb: FieldBaseline) -> list[Signal]:
    """5. Value-distribution drift: numeric KS + median shift, categorical TVD,
    free-text length and token overlap. Catches 'right shape, wrong node'."""
    present = [_norm(v) for v in values if v is not None]
    if not present or not fb.sample:
        return []
    nums, base_nums = _numbers(present), _numbers(fb.sample)

    if fb.numeric and nums and base_nums:
        ks = _ks(nums, base_nums)
        med = _median(nums)
        scale = fb.iqr or abs(fb.median or 0) or 1.0
        shift = abs(med - (fb.median or 0)) / scale
        mag = _clamp(max((ks - 0.4) / 0.6, (shift - 0.25) / 1.0))
        if mag > 0:
            return [Signal(name="value_drift", field=name, magnitude=mag,
                           evidence=f"numeric drift: median {med:g} vs {fb.median:g}, KS={ks:.2f}")]
        return []

    base_distinct = {str(v) for v in fb.sample}
    if fb.distinct_rate < 0.35 and len(base_distinct) <= 12:
        tvd = _tvd(Counter(map(str, present)), Counter(map(str, fb.sample)))
        if tvd > 0.35:
            return [Signal(name="value_drift", field=name, magnitude=_clamp((tvd - 0.35) / 0.65),
                           evidence=f"categorical distribution moved (TVD={tvd:.2f});"
                                    f" now {sorted(set(map(str, present)))[:4]}")]
        return []

    jac = _jaccard(_tokens(present), _tokens(fb.sample))
    length = sum(len(str(v)) for v in present) / len(present)
    lratio = length / (fb.len_mean or 1)
    mag = _clamp(max((0.5 - jac) / 0.5, (abs(math.log2(max(lratio, 1e-3))) - 1.0) / 2.0))
    if mag > 0:
        return [Signal(name="value_drift", field=name, magnitude=mag,
                       evidence=f"text drift: token overlap {jac:.2f} with baseline,"
                                f" mean length {length:.0f} vs {fb.len_mean:.0f}")]
    return []


def _collapse_signal(name, values, fb: FieldBaseline) -> list[Signal]:
    """6. Constant-collapse. The selector now resolves to shared page chrome, so
    every record reports the same value.

    Judged against the baseline on purpose: `books.availability` is genuinely
    constant on a healthy page, and an absolute check flagged it in stage 1."""
    present = [str(_norm(v)) for v in values if v is not None]
    if len(present) < 3 or fb.distinct_rate < 0.35:
        return []
    distinct = len(set(present)) / len(present)
    if distinct >= 0.5 * fb.distinct_rate:
        return []
    top, count = Counter(present).most_common(1)[0]
    return [Signal(name="constant_collapse", field=name,
                   magnitude=_clamp((fb.distinct_rate - distinct) / max(fb.distinct_rate, 0.01)),
                   evidence=f"distinct-value rate {distinct:.0%} vs baseline {fb.distinct_rate:.0%};"
                            f" {count}/{len(present)} records now report {top[:40]!r}")]


def _invariant_signal(name, values, fb: FieldBaseline, base: Baseline) -> list[Signal]:
    """7. Cross-field and domain invariants -- the plausible-garbage catcher."""
    present = [_norm(v) for v in values if v is not None]
    if not present:
        return []
    out: list[Signal] = []
    nums = _numbers(present)
    if fb.all_positive and nums and any(x <= 0 for x in nums):
        bad = sum(1 for x in nums if x <= 0)
        out.append(Signal(name="invariant", field=name, magnitude=_clamp(bad / len(nums)),
                          evidence=f"{bad} non-positive values in a field that was always positive"))
    future = [v for v in present if isinstance(v, str) and _ISO.match(v) and v > _today()]
    if future:
        out.append(Signal(name="invariant", field=name, magnitude=_clamp(len(future) / len(present)),
                          evidence=f"{len(future)} dates in the future (e.g. {future[0]})"))
    # The wrong-node catch: this field now returns values that belong to a
    # *different* field. Reparenting and tag swaps produce exactly this, and the
    # values look entirely plausible in isolation.
    mine = {str(v) for v in present}
    for other, ob in base.fields.items():
        if other == name or not ob.sample:
            continue
        theirs = {str(v) for v in ob.sample}
        if len(mine & theirs) / len(mine) > 0.5 and len(mine & set(map(str, fb.sample))) / len(mine) < 0.5:
            out.append(Signal(name="invariant", field=name, magnitude=0.8,
                              evidence=f"values now match the baseline values of '{other}',"
                                       " not of this field -- locator is on the wrong node"))
            break
    return out


def _novelty_signal(name, values, fb: FieldBaseline) -> list[Signal]:
    """8. Novel-value rate. Deliberately low-weight: a site that publishes new
    content every day scores high here and is perfectly healthy. It earns its
    place as a *discriminator* -- novelty alone means the content changed,
    novelty alongside a tier shift means the structure did."""
    present = [str(_norm(v)) for v in values if v is not None]
    if not present or not fb.sample:
        return []
    seen = {str(v) for v in fb.sample}
    novel = sum(1 for v in present if v not in seen) / len(present)
    if novel < 0.5:
        return []
    return [Signal(name="novel_values", field=name, magnitude=_clamp((novel - 0.5) / 0.5),
                   evidence=f"{novel:.0%} of values never seen in {fb.runs_hint()} baseline sample")]


def _today() -> str:
    from datetime import date
    return date.today().isoformat()


def _runs_hint(self: FieldBaseline) -> str:
    return f"the {len(self.sample)}-value"


FieldBaseline.runs_hint = _runs_hint  # type: ignore[attr-defined]
