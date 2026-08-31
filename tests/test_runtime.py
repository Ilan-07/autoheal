import pytest
from autoheal.runtime import extract, t_money, t_iso_date, check
from autoheal.spec import ExtractorSpec, FieldSpec, Locator, Validator

HTML = """<html><body><ul class="list">
  <li class="row"><span class="was">$99.00</span><span class="price">$24.99</span>
      <h4 class="name">Widget</h4><em class="meta">Qty: 7</em></li>
  <li class="row"><span class="price">bogus</span><h4 class="name">Gadget</h4>
      <em class="meta">Qty: 3</em></li>
</ul></body></html>"""


def spec(stack, transform="text", validators=(), root="li.row"):
    return ExtractorSpec(site="t", record_selector=[Locator(kind="css", q=root)],
                         fields={"v": FieldSpec(stack=stack, transform=transform, validators=list(validators))})


@pytest.mark.parametrize("raw,want", [("$24.99", 24.99), ("USD 24,99", 24.99),
                                      ("GBP 1,234.56", 1234.56), ("nope", None)])
def test_money(raw, want):
    assert t_money(raw) == want


def test_iso_date_formats():
    assert t_iso_date("12 March 2024") == t_iso_date("2024-03-12") == "2024-03-12"


def test_stack_falls_through_on_validator_failure():
    """A value that fails validation must NOT be emitted -- the stack keeps
    walking. Emitting it is precisely the silent-garbage failure mode."""
    s = spec([Locator(kind="css", q="span.price"), Locator(kind="regex", q=r"Qty: (\d+)")],
             transform="money", validators=[Validator(type="number", min=1, max=100)])
    run = extract(s, HTML)
    assert run.records[0].fields["v"].value == 24.99 and run.records[0].fields["v"].tier == 0
    # record 1's price is unparseable, so tier 1 serves it -- and we can see that
    assert run.records[1].fields["v"].value == 3 and run.records[1].fields["v"].tier == 1


def test_provenance_records_winning_tier():
    s = spec([Locator(kind="css", q="span.missing"), Locator(kind="css", q="h4.name")])
    run = extract(s, HTML)
    assert [r.fields["v"].tier for r in run.records] == [1, 1]
    assert run.records[0].fields["v"].kind == "css"


def test_broken_selector_is_a_miss_not_a_crash():
    s = spec([Locator(kind="css", q="!!!not a selector"), Locator(kind="css", q="h4.name")])
    assert extract(s, HTML).records[0].fields["v"].value == "Widget"


def test_no_roots_yields_empty_run():
    s = ExtractorSpec(site="t", record_selector=[Locator(kind="css", q="table.nope")],
                      fields={"v": FieldSpec(stack=[Locator(kind="css", q="td")])})
    run = extract(s, HTML)
    assert run.records == [] and run.root_tier is None


def test_attr_extraction_and_validators():
    assert check(Validator(type="regex", pattern=r"^\d+$"), "42")
    assert not check(Validator(type="number", min=0, max=5), 9.0)
    assert not check(Validator(type="enum", values=["a"]), "b")


# --- locator kinds that had no coverage before the stage-1 audit ------------

LD = """<html><head>
<script type="application/ld+json">{"@type":"WebSite","name":"SiteName"}</script>
<script type="application/ld+json">{"@graph":[
  {"@type":"Product","name":"A","offers":{"price":"1.50"}},
  {"@type":"Product","name":"B","offers":{"price":"2.50"}}]}</script>
</head><body>
  <div class="p"><b>A</b><span>Price: 1.50</span></div>
  <div class="p"><b>B</b><span>Price: 2.50</span></div>
</body></html>"""


def test_jsonld_ignores_non_record_objects():
    """A leading WebSite/Organization node used to shift every record by one and
    return confident wrong values -- the project's own failure mode, in our code."""
    s = spec([Locator(kind="jsonld", q="name")], root="div.p")
    assert [r.fields["v"].value for r in extract(s, LD).records] == ["A", "B"]


def test_jsonld_nested_path():
    s = spec([Locator(kind="jsonld", q="offers.price")], transform="money", root="div.p")
    assert [r.fields["v"].value for r in extract(s, LD).records] == [1.5, 2.5]


def test_jsonld_root_mismatch_is_reported_not_silent():
    s = ExtractorSpec(site="t", record_selector=[Locator(kind="css", q="div.p, body")],
                      fields={"v": FieldSpec(stack=[Locator(kind="jsonld", q="name")])})
    run = extract(s, LD)
    assert any("jsonld/root count mismatch" in e for e in run.errors)


def test_xpath_and_structural_kinds():
    s = spec([Locator(kind="xpath", q="./b")], root="div.p")
    assert extract(s, LD).records[0].fields["v"].value == "A"
    s2 = spec([Locator(kind="structural", q="./*[2]")], root="div.p")
    assert extract(s2, LD).records[0].fields["v"].value == "Price: 1.50"


def test_text_anchor_after_colon():
    s = spec([Locator(kind="text_anchor", q="Price", rel="after_colon")], transform="money", root="div.p")
    assert [r.fields["v"].value for r in extract(s, LD).records] == [1.5, 2.5]


# --- transform and validator edge cases -----------------------------------
# These are the value-producing core: a bug here is silent wrongness by
# definition, because a wrong-but-plausible value passes every downstream check.

@pytest.mark.parametrize("raw,want", [
    ("$24.99", 24.99), ("USD 24,99", 24.99), ("GBP 1,234.56", 1234.56),
    ("1.234,56", 1234.56), ("$1,234,567.89", 1234567.89),
    ("1,234", 1234.0), ("1,23", 1.23), ("-5.50", -5.5), ("£0.01", 0.01),
    ("1 234,56", 1234.56),      # space as a thousands separator; was 1.0
    ("1 234,56", 1234.56),  # NBSP, which is what real pages emit
    ("1e5", None),               # not a price format; was a confident 1.0
    ("", None), ("no digits", None), ("..", None),
])
def test_money_parses_or_refuses(raw, want):
    assert t_money(raw) == want


def test_money_does_not_join_across_a_currency_symbol():
    """The thousands rule must not glue two separate prices together."""
    assert t_money("$1.00 $2.00") == 1.0


@pytest.mark.parametrize("raw,want", [
    ("2024-03-12", "2024-03-12"), ("12 March 2024", "2024-03-12"),
    ("March 12, 2024", "2024-03-12"), ("2024-02-29", "2024-02-29"),
    ("2024-13-45", None),   # month 13, day 45 -- was returned verbatim
    ("2024-02-30", None),   # not a real day in February
    ("not a date", None),
])
def test_iso_date_never_emits_an_impossible_date(raw, want):
    """The regex fallback used to return any yyyy-mm-dd-shaped substring without
    checking it was a date. A refusal falls through to the next locator; a
    plausible wrong date flows downstream forever."""
    assert t_iso_date(raw) == want


def test_number_validator_rejects_booleans():
    """`bool` subclasses `int`, so True satisfied a numeric range check."""
    assert not check(Validator(type="number", min=0, max=5), True)
    assert not check(Validator(type="number", min=0, max=5), False)
    assert check(Validator(type="number", min=0, max=5), 3)


def test_transforms_never_raise_on_hostile_text():
    from autoheal.runtime import TRANSFORMS
    nasty = ["", " ", "\x00", "٣٤", "—", "9" * 400, "nan", "inf", "-", ",", ".", "1/0"]
    for name, fn in TRANSFORMS.items():
        for s in nasty:
            fn(s)  # must not raise


# --- locator kinds and JSON-LD shapes that had no coverage -----------------

def test_url_validator_and_transform():
    assert check(Validator(type="url"), "https://x.test/a")
    assert check(Validator(type="url"), "/relative")
    assert not check(Validator(type="url"), "mailto:a@b.test")
    s = ExtractorSpec(site="t", record_selector=[Locator(kind="css", q="div.r")],
                      fields={"v": FieldSpec(stack=[Locator(kind="css", q="a", attr="href")],
                                             transform="url",
                                             validators=[Validator(type="url")])})
    run = extract(s, "<div class='r'><a href='/p/1'>x</a></div>", base_url="https://shop.test/")
    assert run.records[0].fields["v"].value == "https://shop.test/p/1"


def test_text_anchor_parent_sibling_and_self():
    html = ("<div class='r'><dl><dt>Price</dt></dl><dd>24.99</dd>"
            "<span class='lone'>Total: 5</span></div>")
    s = ExtractorSpec(site="t", record_selector=[Locator(kind="css", q="div.r")],
                      fields={"v": FieldSpec(
                          stack=[Locator(kind="text_anchor", q="Price", rel="parent_sibling_text")],
                          transform="money")})
    assert extract(s, html).records[0].fields["v"].value == 24.99
    s2 = ExtractorSpec(site="t", record_selector=[Locator(kind="css", q="span.lone")],
                       fields={"v": FieldSpec(
                           stack=[Locator(kind="text_anchor", q="Total", rel="self_text")])})
    assert extract(s2, html).records[0].fields["v"].value == "Total: 5"


def test_resolve_returns_the_first_hit_only():
    from autoheal.runtime import Context, resolve, resolve_all
    ctx = Context.build("<div class='r'><b>A</b><b>B</b></div>")
    root = ctx.doc.cssselect("div.r")[0]
    loc = Locator(kind="css", q="b")
    assert resolve_all(loc, root, ctx) == ["A", "B"]
    assert resolve(loc, root, ctx) == "A"
    assert resolve(Locator(kind="css", q="nope"), root, ctx) is None


def test_jsonld_can_serve_as_the_record_selector():
    """A page that ships structured data can be extracted with no DOM records
    at all -- the fallback that keeps `content_deferred` recoverable on shop."""
    html = ("<html><head><script type='application/ld+json'>"
            '[{"@type":"Product","name":"A"},{"@type":"Product","name":"B"}]'
            "</script></head><body></body></html>")
    s = ExtractorSpec(site="t", record_selector=[Locator(kind="css", q="article.none"),
                                                 Locator(kind="jsonld", q="auto")],
                      fields={"v": FieldSpec(stack=[Locator(kind="jsonld", q="name")])})
    run = extract(s, html)
    assert run.root_tier == 1 and [r.fields["v"].value for r in run.records] == ["A", "B"]


def test_jsonld_unwraps_graph_and_itemlist_and_nested_lists():
    from autoheal.runtime import Context
    graph = ("<html><head><script type='application/ld+json'>"
             '{"@graph":[{"@type":"Product","offers":[{"price":"1.50"}]},'
             '{"@type":"Product","offers":[{"price":"2.50"}]}]}'
             "</script></head><body></body></html>")
    assert len(Context.build(graph).jsonld) == 2
    itemlist = ("<html><head><script type='application/ld+json'>"
                '{"itemListElement":[{"@type":"Product","name":"A"},'
                '{"@type":"Product","name":"B"}]}'
                "</script></head><body></body></html>")
    assert len(Context.build(itemlist).jsonld) == 2
    s = ExtractorSpec(site="t", record_selector=[Locator(kind="jsonld", q="auto")],
                      fields={"v": FieldSpec(stack=[Locator(kind="jsonld", q="offers.price")],
                                             transform="money")})
    assert [r.fields["v"].value for r in extract(s, graph).records] == [1.5, 2.5]


def test_jsonld_with_no_repeated_type_is_left_alone():
    """`_align_jsonld` only filters when there is a modal type to filter to."""
    from autoheal.runtime import Context
    html = ("<html><head><script type='application/ld+json'>"
            '[{"@type":"WebSite","name":"S"},{"@type":"Organization","name":"O"}]'
            "</script></head><body></body></html>")
    assert len(Context.build(html).jsonld) == 2


def test_a_transform_that_raises_is_a_miss_and_falls_through():
    from autoheal.runtime import TRANSFORMS
    original = TRANSFORMS.get("money")
    def explode(_s):
        raise ValueError("boom")
    TRANSFORMS["money"] = explode
    try:
        s = ExtractorSpec(site="t", record_selector=[Locator(kind="css", q="div.r")],
                          fields={"v": FieldSpec(stack=[Locator(kind="css", q="b")],
                                                 transform="money")})
        assert extract(s, "<div class='r'><b>1.00</b></div>").records[0].fields["v"].value is None
    finally:
        TRANSFORMS["money"] = original
