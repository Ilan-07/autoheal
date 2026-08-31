"""Extractor specs: the versioned data structure the agent patches.

A spec is *data*, never code. The runtime interprets it deterministically and
nothing model-generated is ever eval'd -- transforms come from a whitelist in
``runtime.TRANSFORMS``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

LocatorKind = Literal[
    "css",  # CSS selector, resolved relative to the record root
    "xpath",  # XPath, relative to the record root
    "jsonld",  # dotted path into document JSON-LD, aligned to the root by index
    "text_anchor",  # find a label node, then step to the value
    "structural",  # tag+position path only, no classes -- survives restyling
    "regex",  # regex over the root's text, group 1
]

# How a text_anchor locator steps from the label node to the value node.
AnchorRel = Literal["after_colon", "next_sibling_text", "parent_sibling_text", "self_text"]


class Locator(BaseModel):
    """One candidate way to find a field. Fields hold an ordered stack of these."""

    kind: LocatorKind
    q: str
    rel: AnchorRel | None = None  # text_anchor only
    attr: str | None = None  # pull this attribute instead of the node's text

    # Provenance. `born` is the spec version that introduced this locator;
    # `last_hit` is the last run in which it was the winning tier. Together they
    # let PERCEIVE notice a silent tier shift and let a reviewer read the history.
    born: int = 1
    last_hit: int | None = None
    note: str | None = None

    def signature(self) -> str:
        return f"{self.kind}:{self.q}:{self.rel or ''}:{self.attr or ''}"


class Validator(BaseModel):
    """Per-value check. A value that fails is treated as a miss, so the stack
    falls through to the next locator rather than emitting plausible garbage."""

    type: Literal["number", "regex", "enum", "nonempty", "max_len", "url"]
    min: float | None = None
    max: float | None = None
    pattern: str | None = None
    values: list[str] | None = None
    max_len: int | None = None


class FieldSpec(BaseModel):
    stack: list[Locator]
    transform: str = "text"
    validators: list[Validator] = Field(default_factory=list)
    # Fraction of records that must carry this field before PERCEIVE complains.
    min_fill: float = 0.95


class ExtractorSpec(BaseModel):
    site: str
    version: int = 1
    parent: int | None = None  # lineage: the version this was patched from
    record_selector: list[Locator]
    fields: dict[str, FieldSpec]
    created_by: str = "human"
    note: str | None = None

    def field_names(self) -> list[str]:
        return list(self.fields.keys())

    def bump(self, *, created_by: str, note: str) -> ExtractorSpec:
        """Return a child spec one version on. Patches are additive; callers
        mutate the copy's stacks rather than editing history in place."""
        child = self.model_copy(deep=True)
        child.parent = self.version
        child.version = self.version + 1
        child.created_by = created_by
        child.note = note
        return child


# --- runtime output -------------------------------------------------------


class FieldResult(BaseModel):
    """One extracted field, with the provenance PERCEIVE needs."""

    value: Any | None = None
    tier: int | None = None  # index into the locator stack that won
    kind: str | None = None
    raw: str | None = None  # pre-transform text, useful as repair evidence
    failed_validator: str | None = None
    # How many nodes the winning locator matched. Observational only -- the
    # value is still the first match -- but a selector that used to match one
    # node per record and now matches two is a decoy, and this is how we see it.
    n_hits: int = 0


class Record(BaseModel):
    fields: dict[str, FieldResult]

    def values(self) -> dict[str, Any]:
        return {k: v.value for k, v in self.fields.items()}

    def tiers(self) -> dict[str, int | None]:
        return {k: v.tier for k, v in self.fields.items()}


class ExtractionRun(BaseModel):
    site: str
    spec_version: int
    run_id: int = 0
    records: list[Record] = Field(default_factory=list)
    n_roots: int = 0
    root_tier: int | None = None
    errors: list[str] = Field(default_factory=list)

    def values(self) -> list[dict[str, Any]]:
        return [r.values() for r in self.records]
