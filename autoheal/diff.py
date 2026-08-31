"""DIAGNOSE, part 1: what structurally changed between two snapshots.

Tree edit distance is a PhD topic and a schedule risk, so this is deliberately
shallow: anchor nodes across the two documents by normalised text hash, then read
off what happened to the anchors that survived. It does not need to be an optimal
edit script -- it needs to *classify* the change well enough to condition a repair
and to key an episode in memory.

The classification is the useful output. `CLASS_RENAME` and `WRAPPER_INSERTED`
call for completely different locator strategies (semantic re-anchor vs. a
class-free structural path), and a wrong guess costs a repair cycle.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Any

from pydantic import BaseModel, Field

from .runtime import parse

CLASS_RENAME = "CLASS_RENAME"
TAG_SWAP = "TAG_SWAP"
WRAPPER_INSERTED = "WRAPPER_INSERTED"
SUBTREE_MOVED = "SUBTREE_MOVED"
ATTR_DROPPED = "ATTR_DROPPED"
CONTENT_DEFERRED = "CONTENT_DEFERRED"
STRUCTURED_DATA_DROPPED = "STRUCTURED_DATA_DROPPED"
RECORDS_REORDERED = "RECORDS_REORDERED"
DECOY_INJECTED = "DECOY_INJECTED"
CONTENT_CHANGED = "CONTENT_CHANGED"
UNKNOWN = "UNKNOWN"

_WS = re.compile(r"\s+")
_HASHED = re.compile(r"^(css-|sc-|jsx-|_)[a-z0-9]{4,}$|^[a-z]{1,3}[-_]?[0-9a-f]{6,}$")


def _norm(s: str | None) -> str:
    return _WS.sub(" ", s or "").strip()


class Edit(BaseModel):
    kind: str
    count: int
    detail: str
    examples: list[str] = Field(default_factory=list)


class DomDiff(BaseModel):
    """A ranked classification, not a minimal edit script."""

    edits: list[Edit] = Field(default_factory=list)
    matched: int = 0
    old_nodes: int = 0
    new_nodes: int = 0

    @property
    def primary(self) -> str:
        return self.edits[0].kind if self.edits else UNKNOWN

    @property
    def classes(self) -> list[str]:
        return [e.kind for e in self.edits]

    def evidence(self) -> list[str]:
        return [f"{e.kind}: {e.detail}" for e in self.edits]

    def summary(self) -> str:
        return " + ".join(f"{e.kind}({e.count})" for e in self.edits) or UNKNOWN


# --- anchoring ------------------------------------------------------------


def _core(s: str) -> str:
    """Comparison key that survives presentation. '$105.24' and '105.24' are the
    same string moving from markup into a JSON blob; comparing them literally
    made `content_deferred` miss its own threshold by one node."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _index(doc) -> tuple[dict[str, list], list, dict[int, int]]:
    """Map normalised text -> the elements that carry it as their own content.

    Leaf-ish nodes only: an ancestor's text_content is the concatenation of its
    children, so indexing every element would anchor a record root to its first
    field and report nonsense."""
    by_text: dict[str, list] = defaultdict(list)
    els: list = []
    pos: dict[int, int] = {}
    for i, el in enumerate(doc.iter()):
        if not isinstance(el.tag, str) or el.tag in ("script", "style"):
            continue
        pos[id(el)] = i
        els.append(el)  # keeps the proxies alive, so the id() keys stay valid
        if len(el) == 0:
            txt = _norm(el.text_content())
            if txt and len(txt) > 1:
                by_text[txt].append(el)
    return by_text, els, pos


def _path(el) -> tuple[str, ...]:
    out = []
    cur = el
    while cur is not None and isinstance(cur.tag, str):
        out.append(cur.tag)
        cur = cur.getparent()
    return tuple(reversed(out))


def _classes(el) -> frozenset[str]:
    return frozenset((el.get("class") or "").split())


def _attrs(el) -> dict[str, str]:
    return {k: v for k, v in el.attrib.items() if k != "class"}


def _jsonld_text(doc) -> set[str]:
    out: set[str] = set()
    for sc in doc.xpath('//script[contains(@type,"json")]'):
        try:
            blob = json.loads(sc.text_content())
        except (ValueError, TypeError):
            continue
        stack: list[Any] = [blob]
        while stack:
            n = stack.pop()
            if isinstance(n, dict):
                stack.extend(n.values())
            elif isinstance(n, list):
                stack.extend(n)
            elif n is not None:
                out.add(_norm(str(n)))
    return out


def diff(old_html: str, new_html: str) -> DomDiff:
    """Classify what happened between two snapshots of the same page."""
    old, new = parse(old_html), parse(new_html)
    old_by_text, old_els, old_pos = _index(old)
    new_by_text, new_els, new_pos = _index(new)
    d = DomDiff(old_nodes=len(old_els), new_nodes=len(new_els))

    pairs = [(old_by_text[t][0], new_by_text[t][0]) for t in old_by_text if t in new_by_text]
    d.matched = len(pairs)

    renamed: list[str] = []
    swapped: list[str] = []
    deepened = 0
    moved = 0
    dropped_attrs: Counter = Counter()

    for o, n in pairs:
        oc, nc = _classes(o), _classes(n)
        if oc != nc:
            gone = sorted(oc - nc)
            gained = sorted(nc - oc)
            # A rename is a swap of tokens; a pure addition is styling noise.
            if gone and gained:
                renamed.append(f"{'.'.join(gone[:2])} -> {'.'.join(gained[:2])}")
        if o.tag != n.tag:
            swapped.append(f"<{o.tag}> -> <{n.tag}>")
        op, np_ = _path(o), _path(n)
        if len(np_) > len(op):
            deepened += 1
        elif op != np_:
            moved += 1
        for k, v in _attrs(o).items():
            if k not in n.attrib:
                dropped_attrs[k] += 1

    # Anchoring only reaches leaf text nodes, so a rename on a *container* class
    # is invisible to the loop above -- which made classification depend on which
    # tokens a given seed happened to pick. A document-level token census catches
    # those, and the two detectors together are seed-stable.
    gone, appeared = _class_census(old, new)
    if renamed or (gone and appeared):
        pairs = renamed or [f"{g} -> {a}" for g, a in zip(sorted(gone)[:4], sorted(appeared)[:4])]
        hashed = sum(1 for t in appeared if _HASHED.match(t))
        d.edits.append(Edit(kind=CLASS_RENAME, count=max(len(renamed), len(gone)),
                            detail=f"{len(renamed)} anchored nodes changed class;"
                                   f" {len(gone)} class tokens vanished and {len(appeared)} appeared"
                                   f"{f' ({hashed} hashed/CSS-in-JS)' if hashed else ''}",
                            examples=pairs[:4]))
    if swapped:
        d.edits.append(Edit(kind=TAG_SWAP, count=len(swapped),
                            detail=f"{len(swapped)} anchored nodes changed tag",
                            examples=sorted(set(swapped))[:4]))
    if deepened:
        d.edits.append(Edit(kind=WRAPPER_INSERTED, count=deepened,
                            detail=f"{deepened} anchored nodes sit deeper than before"
                                   " -- wrapper elements were inserted"))
    if moved:
        d.edits.append(Edit(kind=SUBTREE_MOVED, count=moved,
                            detail=f"{moved} anchored nodes changed ancestor path"))
    if dropped_attrs:
        d.edits.append(Edit(kind=ATTR_DROPPED, count=sum(dropped_attrs.values()),
                            detail=f"attributes removed or renamed: {dict(dropped_attrs.most_common(4))}",
                            examples=list(dropped_attrs)[:4]))

    # Structured data that a fallback tier may have been relying on.
    o_ld, n_ld = len(old.xpath('//script[contains(@type,"json")]')), len(new.xpath('//script[contains(@type,"json")]'))
    o_micro = len(old.xpath("//*[@itemprop]"))
    n_micro = len(new.xpath("//*[@itemprop]"))
    if (o_ld and not n_ld) or (o_micro and not n_micro):
        d.edits.append(Edit(kind=STRUCTURED_DATA_DROPPED, count=(o_ld - n_ld) + (o_micro - n_micro),
                            detail=f"ld+json scripts {o_ld}->{n_ld}, itemprop attrs {o_micro}->{n_micro}"))

    # Visible text gone, but still present in a JSON blob: client-side render.
    lost = set(old_by_text) - set(new_by_text)
    if lost:
        json_cores = {_core(t) for t in _jsonld_text(new)}
        in_json = {t for t in lost if _core(t) in json_cores}
        # The volume guard matters: on a page that ships JSON-LD, *any* content
        # edit leaves the old values sitting in the blob, so string overlap alone
        # classified the churn control as a client-render migration. Deferral
        # means the visible text actually went away.
        vis_old, vis_new = _visible_chars(old), _visible_chars(new)
        deferred = vis_new < 0.6 * vis_old
        if deferred and len(in_json) >= max(3, 0.3 * len(lost)):
            d.edits.append(Edit(kind=CONTENT_DEFERRED, count=len(in_json),
                                detail=f"{len(in_json)} of {len(lost)} vanished strings are still in a JSON blob"
                                       " -- the page went client-rendered",
                                examples=sorted(in_json)[:3]))
        elif len(lost) > 0.3 * len(old_by_text):
            d.edits.append(Edit(kind=CONTENT_CHANGED, count=len(lost),
                                detail=f"{len(lost)} of {len(old_by_text)} text nodes no longer appear anywhere",
                                examples=sorted(lost)[:3]))

    # A decoy clones a field node, so its class token doubles in frequency while
    # the clone's text matches nothing in the old page.
    decoys = _decoys(old, new, set(new_by_text) - set(old_by_text))
    if decoys:
        d.edits.append(Edit(kind=DECOY_INJECTED, count=len(decoys),
                            detail=f"class tokens whose node count grew while carrying unseen text: "
                                   f"{sorted(decoys)[:3]}",
                            examples=sorted(decoys)[:4]))

    if _reordered(old_by_text, new_by_text, old_pos, new_pos):
        d.edits.append(Edit(kind=RECORDS_REORDERED, count=1,
                            detail="surviving anchors appear in a different document order"))

    # Rank by how strongly each class constrains the repair strategy, then size.
    weight = {DECOY_INJECTED: 6, CONTENT_DEFERRED: 5, STRUCTURED_DATA_DROPPED: 4,
              CLASS_RENAME: 3, TAG_SWAP: 3, WRAPPER_INSERTED: 2, SUBTREE_MOVED: 2,
              ATTR_DROPPED: 1, RECORDS_REORDERED: 1, CONTENT_CHANGED: 0}
    d.edits.sort(key=lambda e: (-weight.get(e.kind, 0), -e.count))
    return d


def _class_census(old, new) -> tuple[set[str], set[str]]:
    """Class tokens that disappeared from the document, and ones that appeared."""
    def toks(doc) -> set[str]:
        out: set[str] = set()
        for el in doc.iter():
            if isinstance(el.tag, str):
                out |= _classes(el)
        return out

    o, n = toks(old), toks(new)
    return o - n, n - o


def _decoys(old, new, novel_texts: set[str]) -> set[str]:
    def counts(doc) -> Counter:
        c: Counter = Counter()
        for el in doc.iter():
            if isinstance(el.tag, str):
                c.update(_classes(el))
        return c

    oc, nc = counts(old), counts(new)
    out = set()
    for tok, n in nc.items():
        o = oc.get(tok, 0)
        if o >= 2 and n >= 1.6 * o:
            carries_novel = any(
                _norm(el.text_content()) in novel_texts
                for el in new.iter()
                if isinstance(el.tag, str) and tok in _classes(el) and len(el) == 0
            )
            if carries_novel:
                out.add(tok)
    return out


def _visible_chars(doc) -> int:
    body = doc.find("body")
    return len(_norm((body if body is not None else doc).text_content()))


def _reordered(old_by_text: dict, new_by_text: dict, old_pos: dict, new_pos: dict) -> bool:
    """Kendall-tau over the anchors that survived, on their document positions.

    Elementwise comparison of two sorted lists was the first attempt and it both
    missed a genuine shuffle on `books` and invented one on churned content:
    dropping or adding a single anchor shifts every position after it. Counting
    concordant *pairs* is insensitive to that."""
    common = [t for t in old_by_text if t in new_by_text]
    # Enough survivors, and enough *of* the survivors: on a page whose text was
    # mostly blanked, tau over the handful of remaining anchors is noise, and it
    # reported a shuffle that never happened.
    if len(common) < 10 or len(common) < 0.65 * len(old_by_text):
        return False
    common.sort(key=lambda t: old_pos[id(old_by_text[t][0])])
    seq = [new_pos[id(new_by_text[t][0])] for t in common[:250]]
    pairs = concordant = 0
    for i in range(len(seq)):
        for j in range(i + 1, len(seq)):
            pairs += 1
            concordant += seq[i] < seq[j]
    return bool(pairs) and concordant / pairs < 0.95
