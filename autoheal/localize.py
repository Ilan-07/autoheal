"""DIAGNOSE, part 2: turn known-good values into ranked candidate locators.

This is the module the whole design leans on. The one-shot baseline hands a model
400KB of HTML and asks it to fix a selector. We instead use the fact that we
already know what the answer *was*: yesterday `price` was 24.99, so find 24.99 in
today's DOM, derive every reasonable way to address the node it landed in, and
score those ways against every record on the page. The model's job shrinks from
"read this haystack" to "pick one of eight, and say why".

Everything here is deterministic and testable. If the ranker is good the LLM step
is easy; if it is bad no prompt can save it. So the eval measures top-1 and top-3
accuracy of this module *alone*, with no model in the loop at all.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from lxml import html as lhtml
from pydantic import BaseModel, Field

from .runtime import (LOCATOR_ERRORS, TRANSFORM_ERRORS, TRANSFORMS, Context, check,
                       resolve_all, t_text)
from .spec import ExtractorSpec, FieldSpec, Locator

_HASHED = re.compile(r"^(css-|sc-|jsx-|_)[a-z0-9]{4,}$|^[a-z]{1,3}[-_]?[0-9a-f]{6,}$")
_LAYOUT = re.compile(r"^(col|row|grid|flex|container|wrapper|inner|outer|layout|mt|mb|ml|mr|px|py|p|m)[-_]?\d*$")
_SEMANTIC_ATTRS = ("itemprop", "data-testid", "data-qa", "role", "aria-label")

MAX_ROOTS_FOR_GENERATION = 4
MAX_HITS_PER_ROOT = 3
MAX_CANDIDATES = 8  # what the LLM ever sees; the cost ceiling lives here
MAX_BLIND_NODES_PER_ROOT = 25  # only used when known-good values are withheld
MAX_BLIND_LOCATORS = 240


def _key(v: Any) -> Any:
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        return " ".join(v.split())
    return v


def _core(s: Any) -> str:
    """Match key that ignores presentation: '37,732,000' == 37732000.

    Normalising through `_key` first is load-bearing, not tidiness. The runtime's
    `money` transform returns floats, so a known-good 37732000.0 stringified to
    '37732000.0' and cored to '377320000' -- one digit longer than anything on the
    page. Every numeric field silently generated zero candidates and every page
    with one quarantined, with no error anywhere to say why."""
    return re.sub(r"[^a-z0-9]", "", str(_key(s)).lower())


class Candidate(BaseModel):
    """A proposed locator, with the deterministic evidence that ranks it."""

    locator: Locator
    recovery: float = 0.0  # fraction of records where it reproduces a known-good value
    coverage: float = 0.0  # fraction where it yields any value that passes validation
    prior: float = 0.5  # robustness of the addressing scheme itself
    survives_old: bool = False  # also works on the last-known-good snapshot
    score: float = 0.0
    n_hits_mean: float = 1.0
    source: str = ""

    def explain(self) -> str:
        return (
            f"{self.locator.kind} {self.locator.q!r}"
            + (f" @{self.locator.attr}" if self.locator.attr else "")
            + f" | recovers {self.recovery:.0%} of known values, covers {self.coverage:.0%}"
            f" of records, robustness {self.prior:.2f}"
            + (", also works on the pre-break page" if self.survives_old else "")
        )


# --- strategy classes -----------------------------------------------------


def strategy_of(loc: Locator) -> str:
    """The *kind of idea* a locator represents, independent of site or syntax.

    Episodes are keyed on this rather than on `locator.kind` so that memory can
    transfer. "Site A's class rename was healed by reaching for structured data"
    is a reusable lesson; "site A used a `jsonld` locator with query
    `offers.price`" is not, and keying on the concrete kind made recall little
    more than a same-site cache -- cross-site transfer measured 12% against 51%
    for same-site recall."""
    if loc.kind == "jsonld":
        return "structured_data"
    if loc.attr and (loc.attr.startswith("data-") or loc.attr in ("itemprop", "content", "datetime")):
        return "semantic_attr"
    if loc.kind == "css":
        if ":not(" in loc.q:
            return "exclusion"
        if any(a in loc.q for a in _SEMANTIC_ATTRS):
            return "semantic_attr"
        toks = re.findall(r"[.#\[]([A-Za-z0-9_-]+)", loc.q)
        if toks and all(_HASHED.match(t) for t in toks):
            return "hashed_class"
        if toks and any(_LAYOUT.match(t) for t in toks):
            return "layout_class"
        return "stable_class" if toks else "bare_tag"
    if loc.kind == "text_anchor":
        return "text_anchor"
    if loc.kind == "regex":
        return "regex_shape"
    return "positional"  # structural and xpath both address by position


# --- robustness prior -----------------------------------------------------


def prior(loc: Locator) -> float:
    """How much we trust this *kind* of address to survive the next redesign.

    Semantic attributes outlive class names, which outlive hashed class names,
    which outlive positional paths. This ordering is the difference between a
    repair and a repair that breaks again next week, and it is applied before
    the model is consulted so the model cannot talk us out of it."""
    if loc.kind == "jsonld":
        return 0.90
    if loc.attr and (loc.attr.startswith("data-") or loc.attr in ("itemprop", "content", "datetime")):
        base = 0.90
    elif loc.attr == "href" or loc.attr == "src":
        base = 0.70
    else:
        base = 0.0

    if loc.kind == "css":
        toks = re.findall(r"[.#\[]([A-Za-z0-9_-]+)", loc.q)
        if any(a in loc.q for a in _SEMANTIC_ATTRS):
            k = 0.88
        elif not toks:
            k = 0.40  # bare tag selector
        elif all(_HASHED.match(t) for t in toks):
            k = 0.22  # CSS-in-JS hash: it will be different again next deploy
        elif any(_LAYOUT.match(t) for t in toks):
            k = 0.35  # grid chrome, not a component
        else:
            k = 0.75
    elif loc.kind == "text_anchor":
        k = 0.72  # labels outlive markup, and survive a full restyle
    elif loc.kind == "structural":
        k = 0.45
    elif loc.kind == "xpath":
        k = 0.40
    elif loc.kind == "regex":
        k = 0.50
    else:
        k = 0.40
    return round(max(base, k) if base else k, 3)


# --- record roots ---------------------------------------------------------


def _sibling_groups(doc) -> list[list]:
    groups: dict[tuple, list] = defaultdict(list)
    for el in doc.iter():
        if not isinstance(el.tag, str):
            continue
        p = el.getparent()
        if p is None:
            continue
        groups[(p, el.tag, frozenset((el.get("class") or "").split()))].append(el)
    out = [g for g in groups.values() if len(g) >= 3]
    out.sort(key=lambda g: -len(g) * min(sum(len(_core(e.text_content())) for e in g) / len(g), 200))
    return out


def _discriminated(group: list, parent) -> list[Locator]:
    """Address a sibling group by what it *contains*, when class names cannot.

    A wikitable's data rows are the `tr`s that hold a `td`; the header rows are
    `tr`s that do not. They share a tag and carry no distinguishing class, so
    every class- or child-position-based selector picks up both and caps recovery
    at 39/41 -- close enough to look healthy and never good enough to pass a
    gate. Selecting on a descendant tag that every member has and no excluded
    sibling has expresses the actual distinction."""
    if parent is None:
        return []
    tag = group[0].tag
    members = set(map(id, group))
    others = [c for c in parent if c.tag == tag and id(c) not in members]
    if not others:
        return []

    def descendants(el) -> set[str]:
        return {d.tag for d in el.iter() if isinstance(d.tag, str) and d is not el}

    common = set.intersection(*(descendants(m) for m in group)) if group else set()
    excluded: set[str] = set()
    for o in others:
        excluded |= descendants(o)
    disc = sorted(common - excluded)
    return [Locator(kind="xpath", q=f"//{tag}[.//{d}]",
                    note=f"induced: {tag} elements containing a <{d}>") for d in disc[:2]]


def induce_root_candidates(doc, *, expected_n: int | None = None, values: list[Any] | None = None) -> list[Candidate]:
    """Repeated-subtree induction: find 'the list of things' without a selector.

    Needed whenever the record selector itself broke -- there is nothing to
    localize field values *inside* until we have roots again."""
    cores = {_core(v) for v in (values or []) if v is not None}
    out: list[Candidate] = []
    for group in _sibling_groups(doc)[:6]:
        el = group[0]
        toks = [t for t in sorted((el.get("class") or "").split()) if not _LAYOUT.match(t)]
        locs = []
        if toks:
            locs.append(Locator(kind="css", q=f"{el.tag}.{toks[0]}", note="induced: repeated sibling group"))
        parent = el.getparent()
        ptoks = [t for t in sorted((parent.get("class") or "").split()) if not _LAYOUT.match(t)] if parent is not None else []
        if parent is not None:
            sel = f"{parent.tag}{'.' + ptoks[0] if ptoks else ''} > {el.tag}"
            locs.append(Locator(kind="css", q=sel, note="induced: children of the repeating container"))
        locs += _discriminated(group, parent)
        for loc in locs:
            try:
                hits = doc.cssselect(loc.q) if loc.kind == "css" else doc.xpath(loc.q)
            except LOCATOR_ERRORS:
                continue
            if len(hits) < 3:
                continue
            found = sum(1 for h in hits if any(c and c in _core(h.text_content()) for c in cores)) if cores else 0
            fit = 1.0 if expected_n is None else 1 - min(abs(len(hits) - expected_n) / max(expected_n, 1), 1.0)
            c = Candidate(locator=loc, recovery=found / len(hits) if hits else 0.0,
                          coverage=fit, prior=prior(loc), source="root induction",
                          n_hits_mean=float(len(hits)))
            c.score = round(0.45 * c.recovery + 0.35 * c.coverage + 0.20 * c.prior, 4)
            out.append(c)
    out.sort(key=lambda c: -c.score)
    return _dedupe(out)[:MAX_CANDIDATES]


def find_roots(spec: ExtractorSpec, ctx: Context) -> tuple[list, int | None, list[Candidate]]:
    """Roots via the spec if it still works, otherwise by induction."""
    from .runtime import _find_roots

    roots, tier = _find_roots(spec, ctx)
    if roots:
        return roots, tier, []
    induced = induce_root_candidates(ctx.doc)
    if induced:
        try:
            return list(ctx.doc.cssselect(induced[0].locator.q)), None, induced
        except LOCATOR_ERRORS:
            pass
    return [], None, induced


# --- candidate generation -------------------------------------------------


def _pos_path(node, root) -> str | None:
    parts, cur = [], node
    while cur is not None and cur is not root:
        p = cur.getparent()
        if p is None:
            return None
        parts.append(list(p).index(cur) + 1)
        cur = p
    return "./" + "/".join(f"*[{i}]" for i in reversed(parts)) if cur is root and parts else None


def _tag_path(node, root) -> str | None:
    parts, cur = [], node
    while cur is not None and cur is not root:
        p = cur.getparent()
        if p is None:
            return None
        same = [c for c in p if c.tag == cur.tag]
        parts.append(f"{cur.tag}[{same.index(cur) + 1}]")
        cur = p
    return "./" + "/".join(reversed(parts)) if cur is root and parts else None


def _label_before(node) -> str | None:
    """A visible label a value hangs off: 'Price:' or a preceding <dt>/<th>."""
    prev = node.getprevious()
    for cand in (prev, node.getparent().getprevious() if node.getparent() is not None else None):
        if cand is None or not isinstance(cand.tag, str):
            continue
        txt = " ".join(cand.text_content().split())
        if 2 <= len(txt) <= 40:
            return txt.rstrip(":")
    own = " ".join(node.text_content().split())
    if ":" in own and len(own.split(":", 1)[0]) <= 30:
        return own.split(":", 1)[0]
    return None


def _generate(node, root, attr: str | None) -> list[Locator]:
    """Every reasonable way to address `node` relative to `root`."""
    out: list[Locator] = []
    tag = node.tag if isinstance(node.tag, str) else "*"
    toks = sorted(t for t in (node.get("class") or "").split() if t)

    for t in toks:
        q = f"{tag}.{t}"
        out.append(Locator(kind="css", q=q, attr=attr, note="class of the matched node"))
        out += _disambiguated(q, node, root, attr)
    if node.get("id"):
        out.append(Locator(kind="css", q=f"#{node.get('id')}", attr=attr, note="id of the matched node"))
    for a in _SEMANTIC_ATTRS:
        if node.get(a) is not None:
            out.append(Locator(kind="css", q=f"{tag}[{a}]", attr=attr, note=f"semantic attribute {a}"))
            out.append(Locator(kind="css", q=f'[{a}="{node.get(a)}"]', attr=attr, note=f"{a} value"))
    for a in node.attrib:
        if a.startswith("data-") and a not in _SEMANTIC_ATTRS:
            out.append(Locator(kind="css", q=f"{tag}[{a}]", attr=attr, note=f"data attribute {a}"))
    if attr:  # the value lives in an attribute -- offer the bare tag too
        out.append(Locator(kind="css", q=tag, attr=attr, note=f"first <{tag}> and read @{attr}"))

    sp = _pos_path(node, root)
    if sp:
        out.append(Locator(kind="structural", q=sp, attr=attr, note="class-free positional path"))
    tp = _tag_path(node, root)
    if tp:
        out.append(Locator(kind="xpath", q=tp, attr=attr, note="tag path with indices"))
    if not attr:
        label = _label_before(node)
        if label:
            own = " ".join(node.text_content().split())
            rel = "after_colon" if ":" in own and own.split(":", 1)[0] == label else "next_sibling_text"
            src = node if rel == "after_colon" else (node.getprevious() if node.getprevious() is not None else node)
            out.append(Locator(kind="text_anchor", q=label, rel=rel, note=f"anchored on the label {label!r}"))
    return out


def _disambiguated(q: str, node, root, attr: str | None) -> list[Locator]:
    """When a selector matches the wanted node *and* an impostor ahead of it,
    exclude the impostor by the class it carries and the real node does not.

    This is the repair a human writes for a decoy. The alternative the ranker
    finds on its own is a positional path, which recovers perfectly on the
    broken page and points at the wrong node on the page that used to work --
    so it recovers, then dies on the regression gate, and the site quarantines
    with a correct fix sitting one selector away."""
    try:
        hits = root.cssselect(q)
    except LOCATOR_ERRORS:
        return []
    if len(hits) < 2 or node not in hits or hits[0] is node:
        return []
    mine = set((node.get("class") or "").split())
    extra: set[str] = set()
    for h in hits:
        if h is not node:
            extra |= set((h.get("class") or "").split()) - mine
    return [Locator(kind="css", q=f"{q}:not(.{t})", attr=attr,
                    note=f"excludes a shadowing node marked .{t}")
            for t in sorted(extra)[:2]]


def _blind_generate(roots: list) -> list[Locator]:
    """Enumerate addressable nodes with no idea which one holds the value.

    This is the haystack. With known-good values we look at ~3 nodes per record;
    without them every text-bearing leaf and every value-shaped attribute is a
    candidate, and the ranker has only structural robustness to go on."""
    out: list[Locator] = []
    for root in roots[:MAX_ROOTS_FOR_GENERATION]:
        seen = 0
        for node in root.iter():
            if seen >= MAX_BLIND_NODES_PER_ROOT or len(out) >= MAX_BLIND_LOCATORS:
                break
            if not isinstance(node.tag, str):
                continue
            if len(node) == 0 and " ".join(node.text_content().split()):
                out += _generate(node, root, None)
                seen += 1
            for a in node.attrib:
                if a in ("itemprop", "content", "datetime", "title", "alt", "href") or a.startswith("data-"):
                    out += _generate(node, root, a)
                    seen += 1
                    break
    return out[:MAX_BLIND_LOCATORS]


def _value_regexes(values: list[Any]) -> list[Locator]:
    """A shape-based fallback that ignores markup entirely. Crude, and exactly
    right when a redesign has left no stable node to point at."""
    strs = [str(v) for v in values if v is not None][:20]
    if not strs:
        return []
    out = []
    if all(re.fullmatch(r"-?\d+(\.\d+)?", s) for s in strs):
        dec = max(len(s.split(".")[1]) for s in strs if "." in s) if any("." in s for s in strs) else 0
        out.append(Locator(kind="regex", q=rf"([£$€]\s?\d[\d,]*\.\d{{{dec}}})" if dec else r"(\b\d[\d,]*\b)",
                           note="value shape, markup-independent"))
    prefixes = Counter(s[:6] for s in strs)
    pre, n = prefixes.most_common(1)[0]
    if n >= max(3, 0.6 * len(strs)) and not pre.isdigit():
        out.append(Locator(kind="regex", q=f"({re.escape(pre)}[\\w-]*)", note=f"shared value prefix {pre!r}"))
    return out


def _dedupe(cands: list[Candidate]) -> list[Candidate]:
    seen, out = set(), []
    for c in cands:
        sig = c.locator.signature()
        if sig not in seen:
            seen.add(sig)
            out.append(c)
    return out


# --- evaluation -----------------------------------------------------------


def _evaluate(loc: Locator, roots: list, ctx: Context, fspec: FieldSpec, targets: Counter) -> tuple[float, float, float]:
    """Run a candidate exactly as the runtime would, over every record."""
    fn = TRANSFORMS.get(fspec.transform, t_text)
    recovered = covered = 0
    hits_total = 0
    for i, root in enumerate(roots):
        ctx.root_index = i
        hits = resolve_all(loc, root, ctx)
        hits_total += len(hits)
        if not hits:
            continue
        try:
            value = fn(hits[0])
        except TRANSFORM_ERRORS:
            value = None
        if value is None or any(not check(v, value) for v in fspec.validators):
            continue
        covered += 1
        if targets.get(_key(value), 0):
            recovered += 1
    n = len(roots) or 1
    return recovered / n, covered / n, hits_total / n


def evaluate_locator(
    spec: ExtractorSpec,
    field: str,
    new_html: str,
    known_good: list[Any],
    loc: Locator,
    *,
    old_html: str | None = None,
    base_url: str = "",
    regression_aware: bool = True,
    known_good_aware: bool = True,
) -> Candidate | None:
    """Score a single proposed locator with the same machinery that ranks the
    generated ones. This is what keeps a model's suggestion a *proposal*: it is
    measured on the live page before it can outrank anything."""
    fspec = spec.fields[field]
    ctx = Context.build(new_html, base_url)
    roots, _tier, _ind = find_roots(spec, ctx)
    if not roots:
        return None
    targets = Counter(_key(v) for v in known_good if v is not None)
    rec, cov, hits = _evaluate(loc, roots, ctx, fspec, targets)
    if cov == 0:
        return None
    survives = False
    if old_html and regression_aware:
        old_ctx = Context.build(old_html, base_url)
        old_roots, _t, _i = find_roots(spec, old_ctx)
        if old_roots:
            old_rec, old_cov, _h = _evaluate(loc, old_roots, old_ctx, fspec, targets)
            survives = (old_rec if known_good_aware else old_cov) >= 0.9
    p = prior(loc)
    crowding = 0.15 * min(max(hits - 1.0, 0.0), 1.0)
    # Same two scoring regimes as `candidates`, so a model's proposed widening is
    # judged on exactly the terms its rivals were.
    if known_good_aware:
        score = (0.50 * rec + 0.18 * cov + 0.20 * p
                 + (0.12 * survives if regression_aware else 0.0) - crowding)
    else:
        score = (0.36 * cov + 0.40 * p
                 + (0.24 * survives if regression_aware else 0.0) - crowding)
    return Candidate(
        locator=loc, recovery=round(rec, 4), coverage=round(cov, 4), prior=p,
        survives_old=survives, n_hits_mean=round(hits, 2), source=loc.note or "",
        score=round(score, 4),
    )


def candidates(
    spec: ExtractorSpec,
    field: str,
    new_html: str,
    known_good: list[Any],
    *,
    old_html: str | None = None,
    base_url: str = "",
    limit: int = MAX_CANDIDATES,
    regression_aware: bool = True,
    known_good_aware: bool = True,
) -> list[Candidate]:
    """Rank ways to re-find `field`, given what it used to return.

    `known_good` is the supervision signal -- bet 3 of the design. Without it
    this degenerates into guessing which node on the page looks price-shaped.

    `known_good_aware=False` makes that degeneration concrete, and is the
    `-known-good` ablation: candidates are enumerated from *every* text- or
    attribute-bearing node in the record rather than from the nodes carrying a
    value we already know, and `recovery` is dropped from the score. That is the
    informational position a one-shot "here is the new HTML, fix the selector"
    prompt is in. Recovery is still *measured* so the report can say what the
    blind ranker actually picked -- it just cannot influence the ranking."""
    fspec = spec.fields[field]
    ctx = Context.build(new_html, base_url)
    roots, _tier, _induced = find_roots(spec, ctx)
    if not roots:
        return []

    targets = Counter(_key(v) for v in known_good if v is not None)
    cores = {_core(v) for v in known_good if v is not None}
    raw: list[Locator] = []

    if known_good_aware:
        for root in roots[:MAX_ROOTS_FOR_GENERATION]:
            found = 0
            for node in root.iter():
                if found >= MAX_HITS_PER_ROOT or not isinstance(node.tag, str):
                    continue
                if len(node) == 0:
                    txt = _core(node.text_content())
                    if txt and txt in cores:
                        raw += _generate(node, root, None)
                        found += 1
                        continue
                for a, v in node.attrib.items():
                    if a != "class" and _core(v) and _core(v) in cores:
                        raw += _generate(node, root, a)
                        found += 1
                        break
    else:
        raw += _blind_generate(roots)

    # The stack we already have is a candidate set too: a locator that dropped to
    # tier 3 may still be the most robust address on the page.
    # The existing stack stays available in both arms: the spec is not the
    # supervision signal, and a one-shot prompt would be shown it too.
    raw += [l.model_copy(update={"note": "existing stack entry"}) for l in fspec.stack]
    if known_good_aware:
        # Shape-derived regexes read the known-good values, so they are part of
        # what the ablation withholds.
        raw += _value_regexes(known_good)

    old_ctx, old_roots = None, []
    # `regression_aware=False` is the -regression ablation. It removes the check
    # from *both* places it lives: this ranking term and the G2 gate. Ablating
    # only the gate showed no effect at all, because a candidate that also works
    # on the pre-break page is already preferred here -- so the gate had nothing
    # left to reject. The idea is what earns its place, not the gate alone.
    if old_html and regression_aware:
        old_ctx = Context.build(old_html, base_url)
        old_roots, _t, _i = find_roots(spec, old_ctx)

    scored: list[Candidate] = []
    seen_sigs: set[str] = set()
    for loc in raw:
        # Dedupe before evaluation, not after: the blind arm generates hundreds
        # of near-identical locators and each one costs a pass over every record.
        if loc.signature() in seen_sigs:
            continue
        seen_sigs.add(loc.signature())
        rec, cov, hits = _evaluate(loc, roots, ctx, fspec, targets)
        if cov == 0:
            continue
        survives = False
        if old_ctx and old_roots:
            old_rec, old_cov, _h = _evaluate(loc, old_roots, old_ctx, fspec, targets)
            # With known-good withheld, "survives the old page" has to mean the
            # locator still resolves to *something* valid there -- asking whether
            # it reproduces the known-good values would hand the ablation back
            # the exact signal it is meant to remove.
            survives = (old_rec if known_good_aware else old_cov) >= 0.9
        p = prior(loc)
        # A locator matching several nodes per record is how the decoy won in the
        # first place, so multiplicity is a penalty even when recovery is perfect.
        crowding = 0.15 * min(max(hits - 1.0, 0.0), 1.0)
        if known_good_aware:
            score = (0.50 * rec + 0.18 * cov + 0.20 * p
                     + (0.12 * survives if regression_aware else 0.0) - crowding)
        else:
            # Recovery's 0.50 redistributed proportionally, so the crowding
            # penalty keeps its relative size instead of dominating a half-weight
            # score. Ranking is scale-invariant; readability is not.
            score = (0.36 * cov + 0.40 * p
                     + (0.24 * survives if regression_aware else 0.0) - crowding)
        scored.append(Candidate(locator=loc, recovery=round(rec, 4), coverage=round(cov, 4), prior=p,
                                survives_old=survives, score=round(score, 4), n_hits_mean=round(hits, 2),
                                source=loc.note or ""))

    # Recovery is a tiebreaker only when we are allowed to know it. Leaving it in
    # the sort key was a real leak: the blind arm's ranking still moved when the
    # known-good values changed, so the ablation was measuring less than it claimed.
    if known_good_aware:
        scored.sort(key=lambda c: (-c.score, -c.recovery, -c.prior, c.locator.signature()))
    else:
        scored.sort(key=lambda c: (-c.score, -c.prior, c.locator.signature()))
    return _dedupe(scored)[:limit]
