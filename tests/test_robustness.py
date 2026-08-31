"""Hostile and degenerate inputs must degrade, never crash.

This file exists because the failure mode this project is about -- a pipeline
that keeps running while producing nonsense -- has a twin that is nearly as bad:
a pipeline that dies on a page shape nobody anticipated. Every entry point here
is expected to return something sane for input it cannot make sense of.

The empty-document case is not hypothetical. `lxml` raises
`ParserError: Document is empty` on "" and on whitespace, and an empty response
is an ordinary production event -- a truncated fetch, a 200 with no body, a page
behind a render that failed. Before `runtime.parse` existed, every one of the
seven entry points below raised on it.
"""

import pytest

from autoheal.diff import diff
from autoheal.localize import candidates, induce_root_candidates
from autoheal.loop import heal
from autoheal.perceive import Baseline, perceive
from autoheal.runtime import Context, extract, parse
from eval import mutators
from eval.harness import BASE_URL, SITES, load

HOSTILE = {
    "empty": "",
    "whitespace": "   \n\t  ",
    "not_html": "plain text, no tags at all",
    "unclosed": "<html><body><article class='product-card'><span class=product-price>$1.00",
    "no_records": "<html><body><p>nothing here</p></body></html>",
    "unicode": "<html><body><article class='product-card'>"
               "<span class='product-price'>￥1٬234٫56 “смоки”</span></article></body></html>",
    "deep_nesting": "<html><body>" + "<div>" * 300
                    + "<span class='product-price'>$1</span>" + "</div>" * 300 + "</body></html>",
    "entities": "<html><body><article class='product-card'>"
                "<span class='product-price'>&pound;9.99&nbsp;&amp;</span></article></body></html>",
    "duplicate_ids": "<html><body>" + "<article class='product-card' id='x'>"
                     "<span class='product-price'>$2</span></article>" * 3 + "</body></html>",
    "malformed_jsonld": "<html><head><script type='application/ld+json'>{not json</script></head>"
                        "<body><article class='product-card'><span class='product-price'>$3</span>"
                        "</article></body></html>",
    "control_chars": "<html><body><article class='product-card'>"
                     "<span class='product-price'>\x00\x01</span></article></body></html>",
    "huge_attribute": "<html><body><article class='product-card' data-x='" + "a" * 100_000
                      + "'><span class='product-price'>$4</span></article></body></html>",
}


@pytest.fixture(scope="module")
def shop():
    spec, page = load("shop")
    clean = extract(spec, page, base_url=BASE_URL)
    return spec, page, Baseline.observe(clean), clean.values()


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_every_entry_point_survives_hostile_input(name, shop):
    spec, page, base, kg = shop
    html = HOSTILE[name]
    run = extract(spec, html, base_url=BASE_URL)
    perceive(run, base, spec)
    diff(page, html)
    diff(html, page)  # and in the other direction
    candidates(spec, "price", html, [r.get("price") for r in kg], old_html=page, base_url=BASE_URL)
    induce_root_candidates(Context.build(html).doc, expected_n=24, values=[1, 2])
    heal(spec, broken_html=html, good_html=page, known_good=kg, baseline=base, base_url=BASE_URL)


@pytest.mark.parametrize("blank", ["", "   ", "\n\t\n"])
def test_blank_page_reads_as_zero_records_and_fires_the_monitor(blank, shop):
    """Not merely 'does not crash' -- the *right* behaviour. A blank page must
    look like total breakage and end in an honest quarantine, never a repair."""
    spec, page, base, kg = shop
    run = extract(spec, blank, base_url=BASE_URL)
    assert run.records == [] and run.n_roots == 0
    assert perceive(run, base, spec).fired
    res = heal(spec, broken_html=blank, good_html=page, known_good=kg,
               baseline=base, base_url=BASE_URL)
    assert res.quarantined and not res.healed
    assert res.card and "PAUSED" in res.card


def test_parse_never_raises():
    for bad in ["", "  ", None, "<", "<<<>>>", "\x00"]:
        assert parse(bad) is not None


@pytest.mark.parametrize("site", SITES)
@pytest.mark.parametrize("severity", [1, 2, 3])
def test_pipeline_survives_every_mutator_at_every_severity(site, severity):
    """The combinatorial sweep. 540 mutated pages found no crash; this keeps a
    representative slice of that permanently wired into CI."""
    spec, page = load(site)
    clean = extract(spec, page, base_url=BASE_URL)
    base, kg = Baseline.observe(clean), clean.values()
    for mut in sorted(mutators.MUTATORS):
        broken, log = mutators.apply(page, [mut], seed=severity, severity=severity)
        if log[0].noop:
            continue
        run = extract(spec, broken, base_url=BASE_URL)
        perceive(run, base, spec)
        diff(page, broken)
        for f in spec.field_names():
            candidates(spec, f, broken, [r.get(f) for r in kg],
                       old_html=page, base_url=BASE_URL)


def test_a_locator_error_is_a_miss_but_a_code_bug_is_not_swallowed():
    """Selector evaluation catches `LOCATOR_ERRORS`, not `Exception`.

    A blanket catch there would record a bug in this repo as 'field missing',
    which is precisely the silent failure the project is built to detect."""
    from autoheal.runtime import LOCATOR_ERRORS, resolve_all
    from autoheal.spec import Locator
    ctx = Context.build("<html><body><div class='r'><span>x</span></div></body></html>")
    root = ctx.doc.cssselect("div.r")[0]
    assert resolve_all(Locator(kind="css", q="!!! not a selector"), root, ctx) == []
    assert resolve_all(Locator(kind="xpath", q="///bad["), root, ctx) == []
    assert resolve_all(Locator(kind="regex", q="(unclosed"), root, ctx) == []
    assert Exception not in LOCATOR_ERRORS
