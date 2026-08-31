"""Independent verification of ground truth.

`truth.json` is v1's own output on the clean page, which makes any test of the
form "v1 scores 1.0 against truth" a tautology -- it asserts x == x and can
never fail. These checks are deliberately built on a *different* mechanism than
the extractor: raw-text regex over the HTML, and domain invariants that hold
regardless of how the page is parsed. If v1 mis-extracts, these fail; the F1
test cannot.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Callable

ROOT = pathlib.Path(__file__).resolve().parent

# Derived from disk, not hardcoded. Two literal site tuples used to live in this
# file; adding a site meant remembering to edit both, and forgetting would have
# silently left the new corpus unverified while the run still printed "all
# ground truth verified".
SITE_DIRS = sorted(d.name for d in (ROOT / "sites").iterdir()
                   if d.is_dir() and (d / "truth.json").exists())


def raw(site: str) -> str:
    return (ROOT / "sites" / site / "page.html").read_text()


def truth(site: str) -> list[dict]:
    return json.loads((ROOT / "sites" / site / "truth.json").read_text())


def _col(rows: list[dict], key: str) -> list:
    return [r[key] for r in rows]


# Each check returns (ok, message). Independent of autoheal.runtime by design.
CHECKS: dict[str, list[Callable[[list[dict], str], tuple[bool, str]]]] = {}


def check(site: str):
    def deco(fn):
        CHECKS.setdefault(site, []).append(fn)
        return fn
    return deco


# --- universal -------------------------------------------------------------

def complete(rows, html):
    missing = [k for r in rows for k, v in r.items() if v is None]
    return not missing, f"{len(missing)} null cells"


def not_page_title(rows, html):
    """Classic wrong-node symptom: every record inherits the page/site title."""
    m = re.search(r"<title>(.*?)</title>", html, re.S)
    title = " ".join(m.group(1).split()) if m else ""
    bad = [v for r in rows for v in r.values() if isinstance(v, str) and v == title]
    return not bad, f"{len(bad)} cells equal the page title {title!r}"


# Fields that are legitimately constant on their frozen page. Every book on
# books.toscrape page 1 really is "In stock", so the collapse heuristic fires a
# true false-positive here. Worth carrying into perceive.py: constant-collapse
# must be judged against a *baseline*, not as an absolute rule -- a field that
# has always been constant is not evidence, a field that newly becomes constant
# is. An absolute rule would have made this a permanent false alarm.
CONSTANT_OK = {("books", "availability")}


def not_constant(rows, html, site=""):
    """A field collapsing to one distinct value is the classic wrong-node symptom."""
    if len(rows) < 3:
        return True, ""
    bad = [k for k in rows[0]
           if len({str(r[k]) for r in rows}) == 1 and (site, k) not in CONSTANT_OK]
    return not bad, f"fields constant across all records: {bad}"


for _s in SITE_DIRS:
    for _fn in (complete, not_page_title, not_constant):
        CHECKS.setdefault(_s, []).append(_fn)


# --- per site, cross-checked against the raw HTML --------------------------

@check("books")
def books_prices_match_raw_text(rows, html):
    found = sorted(float(x) for x in re.findall(r"£(\d+\.\d{2})", html))
    return found == sorted(_col(rows, "price")), f"raw £ values {len(found)} vs truth {len(rows)}"


@check("books")
def books_urls_unique(rows, html):
    u = _col(rows, "url")
    return len(set(u)) == len(u) == 20, f"{len(set(u))} unique urls of {len(u)}"


@check("quotes")
def quotes_authors_match_raw_text(rows, html):
    found = sorted(re.findall(r'<small class="author"[^>]*>([^<]+)', html))
    return found == sorted(_col(rows, "author")), f"raw authors {len(found)} vs truth {len(rows)}"


@check("quotes")
def quotes_text_is_quoted(rows, html):
    bad = [t for t in _col(rows, "text") if not (t.startswith("“") and t.endswith("”"))]
    return not bad, f"{len(bad)} quote texts missing their curly quotes"


@check("wikitable")
def wikitable_population_is_descending(rows, html):
    """It is a *ranked* list, so any row swap or off-by-one shows up here."""
    pop = _col(rows, "population")
    bad = [i for i, (a, b) in enumerate(zip(pop, pop[1:])) if a <= b]
    return not bad, f"population not strictly descending at rows {bad[:5]}"


@check("wikitable")
def wikitable_cities_unique(rows, html):
    c = _col(rows, "city")
    return len(set(c)) == len(c), f"{len(c) - len(set(c))} duplicate cities"


@check("shop")
def shop_prices_match_raw_text(rows, html):
    found = sorted(float(x) for x in re.findall(r"\$(\d+\.\d{2})", html))
    return found == sorted(_col(rows, "price")), f"raw $ values {len(found)} vs truth {len(rows)}"


@check("shop")
def shop_skus_unique_and_well_formed(rows, html):
    s = _col(rows, "sku")
    ok = len(set(s)) == len(s) and all(re.fullmatch(r"SKU-\d+", x) for x in s)
    return ok, f"{len(set(s))} unique skus of {len(s)}"


GENERIC = {"complete", "not_page_title", "not_constant"}


def specific_checks(site: str) -> list[str]:
    return [fn.__name__ for fn in CHECKS.get(site, []) if fn.__name__ not in GENERIC]



# --- hn: no structured data, so every check is raw-text or an invariant ----

@check("hn")
def hn_points_match_raw_text(rows, html):
    """The footer also carries `<span class="score">0 points</span>`, so the raw
    list is one longer than the record list -- the stories are the leading 30."""
    found = [int(x) for x in re.findall(r"(\d+) points", html)]
    return found[:len(rows)] == _col(rows, "points"), \
        f"raw point values {found[:3]}... vs truth {_col(rows, 'points')[:3]}..."


@check("hn")
def hn_comments_match_raw_text(rows, html):
    found = [int(x) for x in re.findall(r"(\d+) comments", html)]
    return found == _col(rows, "comments"), f"raw {len(found)} comment counts vs {len(rows)} records"


@check("hn")
def hn_authors_come_from_user_links(rows, html):
    """The nav bar reuses `.hnuser` but its links carry no `id=`, so this also
    catches an extractor that wandered outside the record root."""
    found = re.findall(r"/user\?id=([a-z_]+)", html)
    return found == _col(rows, "author"), f"raw {len(found)} user links vs {len(rows)} records"


@check("hn")
def hn_titles_are_not_nav_labels(rows, html):
    nav = {"new", "past", "ask", "More", "Hacker Digest", "hide"}
    bad = [t for t in _col(rows, "title") if t in nav]
    return not bad, f"titles that are really nav/chrome links: {bad}"


# --- jobs: structured data exists, so cross-check the two representations ---

@check("jobs")
def jobs_posted_matches_the_datetime_attributes(rows, html):
    found = re.findall(r'datetime="(\d{4}-\d{2}-\d{2})"', html)
    return found == _col(rows, "posted"), f"raw {len(found)} datetimes vs {len(rows)} records"


@check("jobs")
def jobs_posted_are_real_calendar_dates(rows, html):
    """Guards the bug that shipped once: a yyyy-mm-dd-shaped string that is not
    a date (month 13, day 45) used to pass straight through the transform."""
    from datetime import date
    bad = []
    for v in _col(rows, "posted"):
        try:
            date.fromisoformat(v)
        except (ValueError, TypeError):
            bad.append(v)
    return not bad, f"impossible dates: {bad}"


@check("jobs")
def jobs_salary_matches_the_lower_bound_in_raw_text(rows, html):
    found = [float(x.replace(",", "")) for x in re.findall(r"\$([\d,]+) &ndash;", html)]
    return found == _col(rows, "salary"), f"raw {len(found)} salary floors vs {len(rows)} records"


@check("jobs")
def jobs_companies_appear_as_the_hiring_organisation(rows, html):
    found = re.findall(r'itemprop="hiringOrganization">([^<]+)<', html)
    return [c.strip() for c in found] == _col(rows, "company"), \
        f"raw {len(found)} organisations vs {len(rows)} records"


@check("jobs")
def jobs_jsonld_agrees_with_the_markup(rows, html):
    """The two representations are independent of each other and of the runtime;
    if the extractor read the wrong one, they disagree."""
    blob = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    graph = json.loads(blob.group(1))["@graph"]
    return ([g["title"] for g in graph] == _col(rows, "title")
            and [g["datePosted"] for g in graph] == _col(rows, "posted")), \
        "JSON-LD titles/dates disagree with the extracted truth"

def verify(site: str) -> list[tuple[str, bool, str]]:
    rows, html = truth(site), raw(site)
    out = []
    for fn in CHECKS[site]:
        res = fn(rows, html, site) if fn is not_constant else fn(rows, html)
        out.append((fn.__name__, *res))
    return out


if __name__ == "__main__":
    failed = 0
    # A site with only the generic checks is not independently verified at all,
    # and would pass this script while proving nothing about its ground truth.
    unchecked = [s for s in SITE_DIRS if not specific_checks(s)]
    if unchecked:
        print(f"\nSITES WITH NO INDEPENDENT CHECK: {unchecked}")
        raise SystemExit(1)
    for site in SITE_DIRS:
        print(f"\n{site}  ({len(truth(site))} records)")
        for name, ok, msg in verify(site):
            print(f"  {'PASS' if ok else 'FAIL'}  {name:34} {'' if ok else msg}")
            failed += not ok
    print(f"\n{'all ground truth verified' if not failed else str(failed) + ' CHECKS FAILED'}\n")
    raise SystemExit(1 if failed else 0)
