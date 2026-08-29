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
