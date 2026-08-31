"""PATCH: spec v(N) -> v(N+1). Additive, reversible, and never executable.

Three rules, all of them deliberate and all of them demoable:

1. **Additive.** A repair *prepends* a locator; it never deletes one. Sites A/B
   test and revert, so the locator that broke today is often the right answer
   again next week -- demoted to a fallback it costs nothing and heals for free.
2. **Bounded.** Stacks are capped, and the entry evicted is the oldest one that
   has never once served a value. History is trimmed by uselessness, not by age.
3. **Data only.** A patch sets `kind`, `q`, `rel`, `attr` and a whitelisted
   transform name. There is no field in which a model could return code, so
   there is nothing to sandbox.
"""

from __future__ import annotations

from pydantic import BaseModel

from .spec import ExtractorSpec, Locator

MAX_STACK = 6


class SpecPatch(BaseModel):
    """The unit of repair. Small enough to print on a slide."""

    field: str | None  # None => the record selector itself
    locator: Locator
    reason: str
    strategy: str = "adopt_candidate"

    def describe(self) -> str:
        where = f"fields.{self.field}" if self.field else "record_selector"
        return f"{where}: promote {self.locator.kind} {self.locator.q!r} to tier 0 -- {self.reason}"


def _promote(stack: list[Locator], loc: Locator, version: int) -> list[Locator]:
    fresh = loc.model_copy(deep=True, update={"born": version, "last_hit": None})
    kept = [l for l in stack if l.signature() != fresh.signature()]
    # Preserve the demoted head's own history; only the new entry is born now.
    out = [fresh] + kept
    if len(out) <= MAX_STACK:
        return out
    dead = [i for i, l in enumerate(out) if i and l.last_hit is None]
    drop = dead[-1] if dead else len(out) - 1
    return out[:drop] + out[drop + 1:]


def apply_patch(spec: ExtractorSpec, patches: list[SpecPatch], *, created_by: str, note: str) -> ExtractorSpec:
    """Return a new spec version. The input is never mutated."""
    child = spec.bump(created_by=created_by, note=note)
    for p in patches:
        if p.field is None:
            child.record_selector = _promote(child.record_selector, p.locator, child.version)
        elif p.field in child.fields:
            child.fields[p.field].stack = _promote(child.fields[p.field].stack, p.locator, child.version)
    return child


def record_hits(spec: ExtractorSpec, run) -> ExtractorSpec:
    """Stamp `last_hit` on the locators that actually served this run.

    This is what makes eviction safe and what lets a reviewer see, months later,
    which fallbacks were ever load-bearing."""
    out = spec.model_copy(deep=True)
    if run.root_tier is not None and run.root_tier < len(out.record_selector):
        out.record_selector[run.root_tier].last_hit = run.run_id
    for rec in run.records:
        for name, res in rec.fields.items():
            if res.tier is not None and name in out.fields and res.tier < len(out.fields[name].stack):
                out.fields[name].stack[res.tier].last_hit = run.run_id
    return out


def spec_diff(old: ExtractorSpec, new: ExtractorSpec) -> list[str]:
    """A human-readable v(N) -> v(N+1) diff. Goes straight onto the dashboard."""
    lines = [f"spec {old.site} v{old.version} -> v{new.version}  ({new.created_by}: {new.note})"]
    lines += _stack_diff("record_selector", old.record_selector, new.record_selector)
    for name in sorted(set(old.fields) | set(new.fields)):
        o = old.fields[name].stack if name in old.fields else []
        n = new.fields[name].stack if name in new.fields else []
        lines += _stack_diff(f"fields.{name}", o, n)
    return lines


def _stack_diff(where: str, old: list[Locator], new: list[Locator]) -> list[str]:
    o_sigs = [l.signature() for l in old]
    n_sigs = [l.signature() for l in new]
    if o_sigs == n_sigs:
        return []
    out = [f"  {where}:"]
    for i, l in enumerate(new):
        sig = l.signature()
        if sig not in o_sigs:
            out.append(f"    + [{i}] {l.kind} {l.q!r}" + (f" @{l.attr}" if l.attr else "") + f"   (born v{l.born})")
        elif o_sigs.index(sig) != i:
            out.append(f"    ~ [{o_sigs.index(sig)}->{i}] {l.kind} {l.q!r}   (demoted, kept as fallback)")
    for l in old:
        if l.signature() not in n_sigs:
            out.append(f"    - {l.kind} {l.q!r}   (evicted: never served a value)")
    return out
