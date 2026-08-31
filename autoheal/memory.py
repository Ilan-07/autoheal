"""REMEMBER: the store, and the episode log that makes memory load-bearing.

Plain files and JSONL on purpose -- everything here is meant to be `cat`-able on
camera and diffable in review. A database would buy nothing and cost trust.

    store/
      extractors/{site}/v{N}.json    versioned specs, with a parent pointer
      snapshots/{site}/{run}.html.gz last-known-good page, and the one that broke
      records/{site}/{run}.json      output + per-field provenance
      baselines/{site}.json          rolling stats for PERCEIVE
      episodes.jsonl                 symptom fingerprint -> what fixed it

`episodes.jsonl` is the transferable asset. It is keyed by **symptom
fingerprint**, not by site, so a repair learned on one site is retrievable when
a different site breaks the same way. That is the difference between memory as a
mechanism and memory as a log file.
"""

from __future__ import annotations

import gzip
import json
import pathlib
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from .perceive import Baseline, BreakageReport
from .spec import ExtractionRun, ExtractorSpec, Locator

Outcome = Literal["healed", "failed", "quarantined"]


# --- symptom fingerprint --------------------------------------------------


class Fingerprint(BaseModel):
    """What a breakage *looks like*, deliberately stripped of site identity.

    Site and field name are excluded so that `shop.price` breaking by class
    rename matches `books.price_color` breaking by class rename. Including them
    would make recall a cache lookup instead of transfer.

    `transform` and `locator_kind` are carried for context and for reading an
    episode back, but they are deliberately *not* scored in `similarity`: they
    are properties of the site's own schema, so weighting them penalised exactly
    the cross-site matches the episode log exists to enable."""

    signals: tuple[str, ...] = ()
    diff_class: str = "unknown"
    transform: str = "text"
    locator_kind: str = "css"

    @classmethod
    def of(cls, report: BreakageReport, field: str, diff_class: str, spec: ExtractorSpec) -> Fingerprint:
        fspec = spec.fields.get(field)
        return cls(
            signals=tuple(sorted({s.name for s in report.fields[field].signals})) if field in report.fields else (),
            diff_class=diff_class,
            transform=fspec.transform if fspec else "text",
            locator_kind=fspec.stack[0].kind if fspec and fspec.stack else "css",
        )

    def key(self) -> str:
        return f"{'+'.join(self.signals)}|{self.diff_class}|{self.transform}|{self.locator_kind}"

    def similarity(self, other: Fingerprint) -> float:
        """0..1, on the two site-independent components only: which signals
        fired, and what the DOM did. A perfect match scores 1.0."""
        a, b = set(self.signals), set(other.signals)
        sig = len(a & b) / len(a | b) if (a or b) else 1.0
        return round(0.65 * sig + 0.35 * (self.diff_class == other.diff_class), 4)


class Episode(BaseModel):
    """One repair attempt, successful or not. Failures are recorded too: the
    loop consults them to avoid re-proposing a strategy that already lost."""

    ts: float = Field(default_factory=time.time)
    site: str
    field: str
    spec_version: int
    fingerprint: Fingerprint
    strategy: str  # exact thing tried, e.g. "adopt_candidate:structural"
    # The transferable half: the *class* of idea, e.g. "structured_data". Recall
    # matches on this so a lesson learned on one site can reach another.
    strategy_class: str = "positional"
    locator: Locator | None = None  # what was promoted to the head of the stack
    outcome: Outcome = "failed"
    cycles: int = 1
    tokens: int = 0
    used_memory: bool = False
    f1_before: float = 0.0
    f1_after: float = 0.0
    gates: dict[str, bool] = Field(default_factory=dict)
    note: str = ""


class Recall(BaseModel):
    episode: Episode
    similarity: float

    def as_prior(self) -> str:
        e = self.episode
        loc = f"{e.locator.kind} {e.locator.q!r}" if e.locator else "n/a"
        return (
            f"similar breakage on '{e.site}.{e.field}' ({self.similarity:.2f} match):"
            f" strategy {e.strategy_class} -> {loc} -> {e.outcome}"
            f" (F1 {e.f1_before:.2f} -> {e.f1_after:.2f}, {e.cycles} cycle(s))"
        )


# --- the store ------------------------------------------------------------


class Store:
    def __init__(self, root: str | pathlib.Path = "store") -> None:
        self.root = pathlib.Path(root)

    def _p(self, *parts: str) -> pathlib.Path:
        p = self.root.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    # -- specs, with lineage
    def save_spec(self, spec: ExtractorSpec) -> pathlib.Path:
        p = self._p("extractors", spec.site, f"v{spec.version}.json")
        p.write_text(json.dumps(spec.model_dump(), indent=1))
        return p

    def spec_versions(self, site: str) -> list[int]:
        d = self.root / "extractors" / site
        if not d.exists():
            return []
        return sorted(int(f.stem[1:]) for f in d.glob("v*.json"))

    def load_spec(self, site: str, version: int | None = None) -> ExtractorSpec:
        vs = self.spec_versions(site)
        if not vs:
            raise FileNotFoundError(f"no specs stored for {site}")
        v = vs[-1] if version is None else version
        return ExtractorSpec(**json.loads((self.root / "extractors" / site / f"v{v}.json").read_text()))

    def lineage(self, site: str) -> list[tuple[int, int | None, str, str]]:
        """(version, parent, created_by, note) -- the audit trail a reviewer reads."""
        out = []
        for v in self.spec_versions(site):
            s = self.load_spec(site, v)
            out.append((s.version, s.parent, s.created_by, s.note or ""))
        return out

    # -- snapshots (gzipped: pages are the bulk of the store)
    def save_snapshot(self, site: str, run_id: int | str, html: str) -> pathlib.Path:
        p = self._p("snapshots", site, f"{run_id}.html.gz")
        p.write_bytes(gzip.compress(html.encode()))
        return p

    def load_snapshot(self, site: str, run_id: int | str) -> str:
        return gzip.decompress((self.root / "snapshots" / site / f"{run_id}.html.gz").read_bytes()).decode()

    def has_snapshot(self, site: str, run_id: int | str) -> bool:
        return (self.root / "snapshots" / site / f"{run_id}.html.gz").exists()

    # -- records + provenance
    def save_records(self, run: ExtractionRun) -> pathlib.Path:
        p = self._p("records", run.site, f"{run.run_id}.json")
        p.write_text(json.dumps(run.model_dump(), indent=1))
        return p

    def load_records(self, site: str, run_id: int | str) -> ExtractionRun:
        return ExtractionRun(**json.loads((self.root / "records" / site / f"{run_id}.json").read_text()))

    # -- the last run we have reason to trust
    def mark_good(self, site: str, run_id: int | str) -> None:
        self._p("good", f"{site}.json").write_text(json.dumps({"run_id": run_id}))

    def last_good(self, site: str) -> Any | None:
        p = self.root / "good" / f"{site}.json"
        return json.loads(p.read_text())["run_id"] if p.exists() else None

    # -- baselines
    def save_baseline(self, b: Baseline) -> pathlib.Path:
        p = self._p("baselines", f"{b.site}.json")
        p.write_text(json.dumps(b.model_dump(), indent=1))
        return p

    def load_baseline(self, site: str) -> Baseline | None:
        p = self.root / "baselines" / f"{site}.json"
        return Baseline(**json.loads(p.read_text())) if p.exists() else None

    # -- episodes
    @property
    def episodes_path(self) -> pathlib.Path:
        return self.root / "episodes.jsonl"

    def append_episode(self, ep: Episode) -> None:
        p = self._p("episodes.jsonl")
        with p.open("a") as fh:
            fh.write(json.dumps(ep.model_dump()) + "\n")

    def episodes(self) -> list[Episode]:
        p = self.episodes_path
        if not p.exists():
            return []
        return [Episode(**json.loads(line)) for line in p.read_text().splitlines() if line.strip()]

    def recall(self, fp: Fingerprint, *, k: int = 3, floor: float = 0.55,
               exclude_site: str | None = None) -> list[Recall]:
        """Retrieve prior repairs that look like this one.

        `exclude_site` is how the eval proves transfer rather than memorisation:
        with it set, a hit can only have come from a *different* site breaking
        the same way."""
        hits = []
        for ep in self.episodes():
            if exclude_site and ep.site == exclude_site:
                continue
            sim = fp.similarity(ep.fingerprint)
            if sim >= floor:
                hits.append(Recall(episode=ep, similarity=sim))
        # Successes first at equal similarity: a failed episode is useful as a
        # warning, but should never outrank a strategy that actually worked.
        hits.sort(key=lambda r: (-r.similarity, r.episode.outcome != "healed", -r.episode.f1_after))
        return hits[:k]

    def failed_strategies(self, site: str, field: str, spec_version: int) -> set[str]:
        """Strategies already tried and lost for this exact field this cycle."""
        return {
            e.strategy
            for e in self.episodes()
            if e.site == site and e.field == field and e.spec_version == spec_version and e.outcome != "healed"
        }
