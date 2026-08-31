"""VERIFY: three gates, all mandatory. A patch that fails any one is not a fix.

G2 is the interesting one. Re-extracting on the page that broke only proves the
patch fits *today's* DOM -- which is exactly what an overfit looks like. Running
the patched spec against the last snapshot that worked is free, deterministic,
and rejects the whole class of repairs that trade tomorrow for today.

G3 closes the loop against the monitor that opened it: if PERCEIVE is still
unhappy, the repair is not done, whatever the F1 says.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .metrics import score
from .perceive import CRITICAL, RECORD, Baseline, BreakageReport, perceive, rescore
from .runtime import extract
from .spec import ExtractorSpec

TAU = 0.90

# Signals about *how* a locator resolved, as opposed to what it returned. On a
# field whose stack was just patched these are structurally uninterpretable: the
# baseline recorded the tier and match-multiplicity of a locator that no longer
# exists, so comparing the replacement against it is apples to oranges. They are
# dropped for patched fields only -- an untouched field with a tier shift still
# blocks the gate, which is the whole reason signal 2 exists.
PROVENANCE_SIGNALS = {"tier_shift", "match_count"}


class Gate(BaseModel):
    name: str
    passed: bool
    detail: str


class Verdict(BaseModel):
    gates: list[Gate] = Field(default_factory=list)
    f1_broken: float = 0.0
    f1_regression: float = 0.0
    report: BreakageReport | None = None

    @property
    def passed(self) -> bool:
        return bool(self.gates) and all(g.passed for g in self.gates)

    def summary(self) -> str:
        return " ".join(f"{'PASS' if g.passed else 'FAIL'}:{g.name}" for g in self.gates)

    def failures(self) -> list[str]:
        return [f"{g.name}: {g.detail}" for g in self.gates if not g.passed]


def verify(
    spec: ExtractorSpec,
    *,
    broken_html: str,
    good_html: str,
    known_good: list[dict],
    baseline: Baseline,
    before: BreakageReport,
    fields: list[str] | None = None,
    patched_fields: set[str] | None = None,
    base_url: str = "",
    tau: float = TAU,
) -> Verdict:
    """Gate a candidate spec. `known_good` is the last output we trust, and is
    the only reference available at repair time -- frozen eval truth is never
    consulted here, or the loop would be grading its own homework."""
    names = fields or spec.field_names()
    v = Verdict()

    run_new = extract(spec, broken_html, base_url=base_url)
    s_new = score(run_new.values(), known_good, names)
    v.f1_broken = round(s_new.macro_f1, 4)
    v.gates.append(Gate(
        name="G1-recovery", passed=s_new.macro_f1 >= tau,
        detail=f"F1 {s_new.macro_f1:.2f} vs last-known-good on the broken page (tau {tau:.2f})"
               f"; {len(run_new.records)} records vs {len(known_good)}"))

    run_old = extract(spec, good_html, base_url=base_url)
    s_old = score(run_old.values(), known_good, names)
    v.f1_regression = round(s_old.macro_f1, 4)
    v.gates.append(Gate(
        name="G2-regression", passed=s_old.macro_f1 >= tau,
        detail=f"F1 {s_old.macro_f1:.2f} re-running the patched spec on the page that used to work"))

    after = perceive(run_new, baseline, spec)
    patched = patched_fields or set()
    for name in patched:
        if name in after.fields:
            after.fields[name] = rescore(
                name, [s for s in after.fields[name].signals if s.name not in PROVENANCE_SIGNALS]
            )
    v.report = after
    was_critical = {f for f, h in before.fields.items() if h.severity == "critical"}
    still = sorted(f for f in was_critical if after.fields.get(f) and after.fields[f].severity == "critical")
    fresh = sorted(
        f for f, h in after.fields.items()
        if h.severity == "critical" and f not in was_critical and f != RECORD
    )
    v.gates.append(Gate(
        name="G3-clearance", passed=not still and not fresh,
        detail=("health signals cleared"
                + (f" (provenance signals waived on patched: {sorted(patched)})" if patched else "")
                if not (still or fresh) else
                f"still critical: {still or '-'}; newly critical: {fresh or '-'}"
                f" (threshold {CRITICAL})")))
    return v
