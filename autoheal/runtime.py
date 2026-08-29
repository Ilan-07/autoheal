"""Deterministic extraction: spec x DOM -> records + provenance.

No LLM is involved here, and none ever should be. This module is the hot path:
a healthy site is scraped for the cost of a parse.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from urllib.parse import urljoin

from lxml import html as lhtml

from .spec import ExtractionRun, ExtractorSpec, FieldResult, Locator, Record, Validator

# --- transforms -----------------------------------------------------------
# Whitelisted by name. The agent may only *reference* these, never define one,
# so a patched spec can never introduce executable code.

_WS = re.compile(r"\s+")
_NUM = re.compile(r"-?\d[\d.,]*")


def _norm(s: str | None) -> str:
    return _WS.sub(" ", (s or "")).strip()


def t_text(s: str) -> str | None:
    return _norm(s) or None


def t_money(s: str) -> float | None:
    """Parse a price out of noisy text: '$24.99', 'USD 24,99', 'GBP 1,234.56'."""
    m = _NUM.search(s or "")
    if not m:
        return None
    tok = m.group(0)
    if "," in tok and "." in tok:
        # Both present: whichever comes last is the decimal separator.
        tok = tok.replace(",", "") if tok.rfind(".") > tok.rfind(",") else tok.replace(".", "").replace(",", ".")
    elif "," in tok:
        # A lone comma with exactly two trailing digits is a decimal comma.
        tail = tok.rsplit(",", 1)[1]
        tok = tok.replace(",", "." if len(tail) == 2 else "")
    try:
        return round(float(tok), 4)
    except ValueError:
        return None


def t_int(s: str) -> int | None:
    m = re.search(r"-?\d+", s or "")
    return int(m.group(0)) if m else None


def t_iso_date(s: str) -> str | None:
    raw = _norm(s)
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d %B %Y", "%B %d, %Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    m = re.search(r"\d{4}-\d{2}-\d{2}", raw)
    return m.group(0) if m else None


def t_digits(s: str) -> str | None:
    d = re.sub(r"\D", "", s or "")
    return d or None


TRANSFORMS = {
    "text": t_text,
    "strip": t_text,
    "money": t_money,
    "int": t_int,
    "float": lambda s: t_money(s),
    "iso_date": t_iso_date,
    "digits": t_digits,
    "url": t_text,
}


# --- validators -----------------------------------------------------------


def check(v: Validator, value: Any) -> bool:
    if value is None:
        return False
    if v.type == "number":
        if not isinstance(value, (int, float)):
            return False
        if v.min is not None and value < v.min:
            return False
        if v.max is not None and value > v.max:
            return False
        return True
    if v.type == "nonempty":
        return bool(str(value).strip())
    if v.type == "regex":
        return bool(v.pattern and re.search(v.pattern, str(value)))
    if v.type == "enum":
        return bool(v.values and str(value) in v.values)
    if v.type == "max_len":
        return v.max_len is None or len(str(value)) <= v.max_len
    if v.type == "url":
        return str(value).startswith(("http://", "https://", "/"))
    return True


# --- resolution context ---------------------------------------------------


@dataclass
class Context:
    """Everything document-level that locators may need, computed once."""

    doc: Any
    base_url: str = ""
    jsonld: list[dict] = field(default_factory=list)  # aligned to record roots
    jsonld_all: list[dict] = field(default_factory=list)  # everything found
    root_index: int = 0

    @classmethod
    def build(cls, html_text: str, base_url: str = "") -> Context:
        doc = lhtml.fromstring(html_text)
        found = _collect_jsonld(doc)
        return cls(doc=doc, base_url=base_url, jsonld=_align_jsonld(found), jsonld_all=found)


def _collect_jsonld(doc) -> list[dict]:
    """Flatten every ld+json / application+json blob into a positional list.

    Positional is the point: the i-th record root is matched to the i-th object,
    which is what makes a 'content moved into a JSON blob' breakage recoverable.
    """
    out: list[dict] = []
    for script in doc.xpath('//script[contains(@type,"json")]'):
        try:
            data = json.loads(script.text_content())
        except (ValueError, TypeError):
            continue
        stack = [data]
        while stack:
            node = stack.pop(0)
            if isinstance(node, list):
                stack = list(node) + stack
            elif isinstance(node, dict):
                if "@graph" in node and isinstance(node["@graph"], list):
                    stack = list(node["@graph"]) + stack
                elif "itemListElement" in node and isinstance(node["itemListElement"], list):
                    stack = list(node["itemListElement"]) + stack
                else:
                    out.append(node)
    return out


def _align_jsonld(objs: list[dict]) -> list[dict]:
    """Keep only the modal @type, so record N maps to product N.

    Positional alignment without this filter is a silent-failure bug of exactly
    the kind this project exists to catch: almost every real page carries a
    leading WebSite/Organization/BreadcrumbList node, which shifts every record
    by one and yields confident, plausible, wrong values. Found in our own
    runtime during the stage-1 audit.
    """
    if not objs:
        return []
    counts: dict[str, int] = {}
    for o in objs:
        ty = o.get("@type")
        ty = ty[0] if isinstance(ty, list) and ty else ty
        if isinstance(ty, str):
            counts[ty] = counts.get(ty, 0) + 1
    if not counts:
        return objs
    modal = max(sorted(counts), key=lambda k: counts[k])  # sorted() => stable ties
    if counts[modal] < 2:
        return objs

    def is_modal(o: dict) -> bool:
        ty = o.get("@type")
        ty = ty[0] if isinstance(ty, list) and ty else ty
        return ty == modal

    return [o for o in objs if is_modal(o)]


def _dig(obj: Any, path: str) -> Any:
    """Dotted path into nested JSON: 'offers.price', 'item.name'."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            cur = cur[0] if cur else None
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    if isinstance(cur, list):
        cur = cur[0] if cur else None
    return cur


# --- locator resolution ---------------------------------------------------


def _node_text(node, attr: str | None) -> str | None:
    if attr:
        val = node.get(attr)
        return _norm(val) if val is not None else None
    return _norm(node.text_content())


def _anchor_value(node, rel: str | None) -> str | None:
    if rel == "self_text":
        return _norm(node.text_content())
    if rel == "after_colon":
        txt = _norm(node.text_content())
        return _norm(txt.split(":", 1)[1]) if ":" in txt else None
    if rel == "next_sibling_text":
        nxt = node.getnext()
        return _norm(nxt.text_content()) if nxt is not None else None
    if rel == "parent_sibling_text":
        parent = node.getparent()
        nxt = parent.getnext() if parent is not None else None
        return _norm(nxt.text_content()) if nxt is not None else None
    return None


def resolve(loc: Locator, root: Any, ctx: Context) -> str | None:
    """Return the raw string a locator finds under `root`, or None."""
    try:
        if loc.kind == "jsonld":
            if not (0 <= ctx.root_index < len(ctx.jsonld)):
                return None
            val = _dig(ctx.jsonld[ctx.root_index], loc.q)
            return _norm(str(val)) if val is not None else None

        if loc.kind == "css":
            hits = root.cssselect(loc.q)
        elif loc.kind in ("xpath", "structural"):
            hits = root.xpath(loc.q)
        elif loc.kind == "text_anchor":
            needle = loc.q.lower()
            hits = [
                n
                for n in root.iter()
                if isinstance(n.tag, str) and needle in _norm(n.text_content()).lower()[: len(needle) + 24]
            ]
            for n in hits:
                got = _anchor_value(n, loc.rel)
                if got:
                    return got
            return None
        elif loc.kind == "regex":
            m = re.search(loc.q, root.text_content() if hasattr(root, "text_content") else str(root))
            return _norm(m.group(1) if m.groups() else m.group(0)) if m else None
        else:
            return None
    except Exception:  # a broken selector is a miss, not a crash
        return None

    for h in hits:
        if isinstance(h, str):
            got = _norm(h)
        elif hasattr(h, "text_content"):
            got = _node_text(h, loc.attr)
        else:
            got = _norm(str(h))
        if got:
            return got
    return None


def _find_roots(spec: ExtractorSpec, ctx: Context) -> tuple[list[Any], int | None]:
    for tier, loc in enumerate(spec.record_selector):
        try:
            if loc.kind == "css":
                hits = ctx.doc.cssselect(loc.q)
            elif loc.kind in ("xpath", "structural"):
                hits = ctx.doc.xpath(loc.q)
            elif loc.kind == "jsonld":
                hits = list(ctx.jsonld)
            else:
                continue
        except Exception:
            continue
        if hits:
            return list(hits), tier
    return [], None


def extract(spec: ExtractorSpec, html_text: str, *, base_url: str = "", run_id: int = 0) -> ExtractionRun:
    """Run a spec over a page. Every field records which locator tier served it."""
    ctx = Context.build(html_text, base_url)
    roots, root_tier = _find_roots(spec, ctx)
    run = ExtractionRun(site=spec.site, spec_version=spec.version, run_id=run_id, n_roots=len(roots), root_tier=root_tier)

    if ctx.jsonld and roots and len(ctx.jsonld) != len(roots):
        run.errors.append(
            f"jsonld/root count mismatch: {len(ctx.jsonld)} objects vs {len(roots)} roots"
            " -- index alignment is unsafe for jsonld locators"
        )

    for i, root in enumerate(roots):
        ctx.root_index = i
        out: dict[str, FieldResult] = {}
        for name, fspec in spec.fields.items():
            out[name] = _extract_field(fspec, root, ctx)
        run.records.append(Record(fields=out))
    return run


def _extract_field(fspec, root: Any, ctx: Context) -> FieldResult:
    """Walk the locator stack; first value that survives validation wins."""
    fn = TRANSFORMS.get(fspec.transform, t_text)
    last_fail: str | None = None

    for tier, loc in enumerate(fspec.stack):
        raw = resolve(loc, root, ctx)
        if raw is None:
            continue
        try:
            value = fn(raw)
        except Exception:
            value = None
        if value is None:
            continue
        if fspec.transform == "url" and ctx.base_url:
            value = urljoin(ctx.base_url, str(value))

        bad = next((v for v in fspec.validators if not check(v, value)), None)
        if bad is not None:
            last_fail = bad.type  # keep falling through -- do not emit garbage
            continue
        return FieldResult(value=value, tier=tier, kind=loc.kind, raw=raw)

    return FieldResult(value=None, tier=None, kind=None, failed_validator=last_fail)
