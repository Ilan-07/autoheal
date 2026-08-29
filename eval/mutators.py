"""Seeded, composable DOM mutations that stand in for a site redesign.

Design constraint: mutators are **site-agnostic**. None of them is handed the
extractor spec, and none is hand-tuned per site -- they locate the repeating
record container heuristically and deform it. Otherwise the eval would be
rigged in our favour, since we would be breaking exactly what we know how to fix.
"""

from __future__ import annotations

import random
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable

from lxml import html as lhtml

HASH_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


@dataclass
class Mutation:
    name: str
    severity: int
    detail: str
    changed: int = 0  # 0 => this mutator is a no-op on this page, so skip the case

    @property
    def noop(self) -> bool:
        return self.changed == 0


# --- shared page analysis -------------------------------------------------


def find_record_container(doc) -> list:
    """Return the largest set of sibling elements sharing a tag+class signature.

    This is how a human eyeballs 'the list of things' on a page, and it lets a
    mutator deform records without being told what the extractor targets.
    """
    groups: dict[tuple, list] = defaultdict(list)
    for el in doc.iter():
        if not isinstance(el.tag, str):
            continue
        parent = el.getparent()
        if parent is None:
            continue
        # Key on the parent element itself, not id(): lxml proxies can be
        # garbage collected and their ids reused, silently merging groups.
        sig = (parent, el.tag, frozenset((el.get("class") or "").split()))
        groups[sig].append(el)

    def score(items: list) -> float:
        if len(items) < 3:
            return 0.0
        text = sum(len(" ".join(i.text_content().split())) for i in items) / len(items)
        return len(items) * min(text, 200)

    best = max(groups.values(), key=score, default=[])
    return best if score(best) > 0 else []


def _field_like_tokens(doc) -> list[str]:
    """Class tokens that name a *field*: carried by exactly one non-root,
    text-bearing element inside every record. `price_color` qualifies;
    `col-md-3` (on the root) and `icon-star` (five per record) do not.

    This distinction is the whole point -- a redesign renames the component,
    not the grid, and an eval that renames the grid proves nothing.
    """
    roots = find_record_container(doc)
    if not roots:
        return []
    hits: dict[str, list[int]] = defaultdict(list)
    for root in roots:
        per_token: dict[str, int] = defaultdict(int)
        for el in root.iter():
            if el is root or not isinstance(el.tag, str):
                continue
            if not " ".join(el.text_content().split()):
                continue
            for tok in (el.get("class") or "").split():
                per_token[tok] += 1
        for tok, n in per_token.items():
            hits[tok].append(n)
    # sorted(): the caller feeds this to rng.shuffle, so a non-deterministic
    # order here silently breaks reproducibility even with a fixed seed.
    return sorted(tok for tok, ns in hits.items() if len(ns) == len(roots) and max(ns) == 1)


def _class_tokens(doc) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for el in doc.iter():
        if isinstance(el.tag, str):
            for tok in (el.get("class") or "").split():
                counts[tok] += 1
    return counts


def _hashed(rng: random.Random) -> str:
    return "css-" + "".join(rng.choice(HASH_ALPHABET) for _ in range(7))


def _perturb(text: str, rng: random.Random) -> str:
    """Make a plausible *wrong* value -- the kind a human would not notice."""
    m = re.search(r"\d[\d,]*\.?\d*", text)
    if m:
        try:
            n = float(m.group(0).replace(",", ""))
            return text[: m.start()] + f"{n * 1.35:,.2f}".rstrip("0").rstrip(".") + text[m.end() :]
        except ValueError:
            pass
    return "Sponsored: " + text


# --- mutators -------------------------------------------------------------
# Each takes (doc, rng, severity) and returns (detail, n_changed).


def class_rename(doc, rng: random.Random, severity: int) -> tuple[str, int]:
    """Semantic class names -> CSS-in-JS hashes. The single most common breakage."""
    counts = _class_tokens(doc)
    field_like = set(_field_like_tokens(doc))
    cands = [t for t, c in counts.items() if 2 <= c <= 400 and not t.startswith("css-")]
    # field-bearing tokens first, then layout chrome, each by descending frequency
    cands.sort(key=lambda t: (t not in field_like, -counts[t]))
    n = {1: 3, 2: 8, 3: 25}[severity]
    chosen = cands[:n]
    mapping = {t: _hashed(rng) for t in chosen}
    for el in doc.iter():
        if isinstance(el.tag, str) and el.get("class"):
            el.set("class", " ".join(mapping.get(t, t) for t in el.get("class").split()))
    return f"renamed {len(mapping)} class tokens -> hashed ({', '.join(list(mapping)[:4])}...)", len(mapping)


def tag_swap(doc, rng: random.Random, severity: int) -> tuple[str, int]:
    """table -> div grid, ul/ol -> div stack. Destroys every positional path."""
    swaps = {"table": "div", "thead": "div", "tbody": "div", "tr": "div", "td": "div",
             "th": "div", "ul": "div", "ol": "div", "li": "div", "article": "section"}
    roles = {"tr": "row", "td": "cell", "th": "columnheader", "li": "listitem"}
    depth = {1: ("tr", "td", "th"), 2: ("table", "tbody", "tr", "td", "th"),
             3: tuple(swaps)}[severity]
    n = 0
    for el in list(doc.iter()):
        if isinstance(el.tag, str) and el.tag in depth and el.tag in swaps:
            if el.tag in roles:
                el.set("role", roles[el.tag])
            el.set("class", " ".join(filter(None, [el.get("class"), f"x-{el.tag}"])))
            el.tag = swaps[el.tag]
            n += 1
    return f"swapped {n} nodes ({'/'.join(depth[:3])} -> div/section, role= attrs added)", n


def reparent(doc, rng: random.Random, severity: int) -> tuple[str, int]:
    """Insert wrapper layers inside each record. Shifts every relative path."""
    roots = find_record_container(doc)
    if not roots:
        return "no record container found; no-op", 0
    layers = severity
    for root in roots:
        for _ in range(layers):
            children = list(root)
            if not children:
                break
            wrap = lhtml.Element("div", attrib={"class": "layout-inner"})
            root.insert(0, wrap)
            for ch in children:
                wrap.append(ch)
    return f"wrapped children of {len(roots)} records in {layers} x div.layout-inner", len(roots)


def attr_migration(doc, rng: random.Random, severity: int) -> tuple[str, int]:
    """Rename/drop the data-* and semantic attributes extractors lean on."""
    renamed = dropped = 0
    for el in doc.iter():
        if not isinstance(el.tag, str):
            continue
        for name in list(el.attrib):
            if name.startswith("data-"):
                el.set("data-qa-" + name[5:], el.attrib.pop(name))
                renamed += 1
            elif name == "itemprop" and severity >= 2:
                el.set("data-prop", el.attrib.pop(name))
                renamed += 1
            elif name == "title" and severity >= 3 and el.tag == "a":
                el.attrib.pop(name)
                dropped += 1
    return f"migrated {renamed} attributes (data-* -> data-qa-*, itemprop -> data-prop), dropped {dropped}", renamed + dropped


def decoy_injection(doc, rng: random.Random, severity: int) -> tuple[str, int]:
    """THE silent failure. Clone a field-bearing node, give it a plausible wrong
    value, and insert it *ahead* of the real one so first-match selectors take it.

    Modelled on a real and very common redesign: a shop adds a struck-through
    'compare at' price above the real one. Nothing errors. Fill rate stays 100%.
    Every downstream number is quietly wrong.
    """
    roots = find_record_container(doc)
    if not roots:
        return "no record container found; no-op", 0

    # Field-bearing tokens only. This used to be a separate, weaker check that
    # accepted any token present in every record, so roughly half of all runs
    # decoyed layout chrome (icon-star, col-lg-3) and hit no extractor at all.
    field_tokens = _field_like_tokens(doc)
    if not field_tokens:
        return "no per-record field class found; no-op", 0
    rng.shuffle(field_tokens)
    targets = field_tokens[: {1: 1, 2: 2, 3: 3}[severity]]

    n = 0
    for root in roots:
        for tok in targets:
            hits = root.cssselect(f".{tok}")
            if not hits:
                continue
            real = hits[0]
            decoy = lhtml.fromstring(lhtml.tostring(real))
            decoy.set("class", (decoy.get("class") or "") + " was-value")
            for node in decoy.iter():
                if node.text and node.text.strip():
                    node.text = _perturb(node.text, rng)
            parent = real.getparent()
            parent.insert(list(parent).index(real), decoy)
            n += 1
    return f"injected {n} decoys on {targets} (plausible wrong values, ahead of the real node)", n


def content_deferred(doc, rng: random.Random, severity: int) -> tuple[str, int]:
    """SSR -> client render: strip the visible text, leave the JSON blob.

    Only bites pages that ship structured data; on pages without it this is a
    total loss and the right behaviour is to quarantine, not to hallucinate.
    """
    roots = find_record_container(doc)
    if not roots:
        return "no record container found; no-op", 0
    n = 0
    for root in roots:
        for el in root.iter():
            if isinstance(el.tag, str) and el.text and el.text.strip():
                el.text = ""
                n += 1
    return f"blanked visible text in {len(roots)} records ({n} nodes); structured data left intact", n


def jsonld_drop(doc, rng: random.Random, severity: int) -> tuple[str, int]:
    """Remove the structured data a fallback tier was quietly relying on.

    Without this the eval flatters us: a page with good JSON-LD survives almost
    any visual redesign via tier 2 and never exercises the repair loop at all.
    """
    n = 0
    for script in doc.xpath('//script[contains(@type,"json")]'):
        script.getparent().remove(script)
        n += 1
    if severity >= 2:
        for el in doc.iter():
            if isinstance(el.tag, str):
                for attr in ("itemprop", "itemtype", "itemscope"):
                    if attr in el.attrib:
                        el.attrib.pop(attr)
                        n += 1
    return f"removed {n} structured-data carriers (ld+json scripts, microdata attrs)", n


def record_reorder(doc, rng: random.Random, severity: int) -> tuple[str, int]:
    """Shuffle record order. Breaks anything aligning DOM to JSON by index."""
    roots = find_record_container(doc)
    if len(roots) < 2:
        return "no record container found; no-op", 0
    parent = roots[0].getparent()
    if any(r.getparent() is not parent for r in roots):
        return "records do not share a parent; no-op", 0
    idx = [list(parent).index(r) for r in roots]
    shuffled = list(roots)
    rng.shuffle(shuffled)
    for i, node in zip(sorted(idx), shuffled):
        parent.insert(i, node)
    return f"reordered {len(roots)} records in the DOM", len(roots)


MUTATORS: dict[str, Callable] = {
    "jsonld_drop": jsonld_drop,
    "record_reorder": record_reorder,
    "class_rename": class_rename,
    "tag_swap": tag_swap,
    "reparent": reparent,
    "attr_migration": attr_migration,
    "decoy_injection": decoy_injection,
    "content_deferred": content_deferred,
}


def apply(html_text: str, names: list[str], *, seed: int = 0, severity: int = 2) -> tuple[str, list[Mutation]]:
    """Apply mutators in order to a page. Deterministic given (names, seed, severity)."""
    doc = lhtml.fromstring(html_text)
    rng = random.Random(seed)
    log = []
    for name in names:
        detail, changed = MUTATORS[name](doc, rng, severity)
        log.append(Mutation(name=name, severity=severity, detail=detail, changed=changed))
    return lhtml.tostring(doc, encoding="unicode"), log
