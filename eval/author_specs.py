"""Authors the day-1 (v1) extractor specs -- what a competent engineer would
write before anything broke. Deliberately NOT tuned against the mutators: the
stacks hold a primary selector plus whatever semantic fallback the page really
offers. Which mutations the stack absorbs for free is a finding, not a setup.
"""
import json, pathlib
from autoheal.spec import ExtractorSpec, FieldSpec, Locator, Validator

L = Locator
V = Validator
SITES = {}

SITES["books"] = ExtractorSpec(
    site="books",
    record_selector=[L(kind="css", q="article.product_pod"), L(kind="xpath", q='//section//ol/li//article')],
    fields={
        "title": FieldSpec(
            stack=[L(kind="css", q="h3 > a", attr="title"), L(kind="css", q="h3 a"),
                   L(kind="structural", q="./h3/a")],
            validators=[V(type="nonempty"), V(type="max_len", max_len=200)]),
        "price": FieldSpec(
            stack=[L(kind="css", q="p.price_color"), L(kind="css", q=".product_price .price_color"),
                   L(kind="regex", q=r"[£$]\s*(\d+\.\d{2})")],
            transform="money", validators=[V(type="number", min=0.5, max=500)]),
        "availability": FieldSpec(
            stack=[L(kind="css", q="p.instock.availability"), L(kind="css", q=".product_price p.instock")],
            validators=[V(type="nonempty")]),
        "url": FieldSpec(
            stack=[L(kind="css", q="h3 > a", attr="href"), L(kind="css", q="div.image_container a", attr="href")],
            transform="url", validators=[V(type="nonempty")]),
    })

SITES["quotes"] = ExtractorSpec(
    site="quotes",
    record_selector=[L(kind="css", q="div.quote"), L(kind="xpath", q='//div[@itemtype="http://schema.org/CreativeWork"]')],
    fields={
        "text": FieldSpec(
            stack=[L(kind="css", q="span.text"), L(kind="xpath", q='.//*[@itemprop="text"]')],
            validators=[V(type="nonempty"), V(type="max_len", max_len=600)]),
        "author": FieldSpec(
            stack=[L(kind="css", q="small.author"), L(kind="xpath", q='.//*[@itemprop="author"]'),
                   L(kind="text_anchor", q="by", rel="next_sibling_text")],
            validators=[V(type="nonempty"), V(type="max_len", max_len=80)]),
        "tags": FieldSpec(
            stack=[L(kind="css", q="meta.keywords", attr="content"), L(kind="xpath", q='.//*[@itemprop="keywords"]', attr="content")],
            validators=[V(type="nonempty")]),
    })

SITES["wikitable"] = ExtractorSpec(
    site="wikitable",
    record_selector=[L(kind="xpath", q='//table[contains(@class,"wikitable")]//tr[td]'),
                     L(kind="css", q="table.wikitable tr")],
    fields={
        "city": FieldSpec(
            stack=[L(kind="xpath", q="./th//a"), L(kind="xpath", q="./th"), L(kind="structural", q="./th[1]")],
            validators=[V(type="nonempty"), V(type="max_len", max_len=60)]),
        "country": FieldSpec(
            stack=[L(kind="xpath", q="./td[1]//a[@title]", attr="title"), L(kind="xpath", q="./td[1]"),
                   L(kind="structural", q="./td[1]")],
            validators=[V(type="nonempty"), V(type="max_len", max_len=60)]),
        "population": FieldSpec(
            stack=[L(kind="xpath", q="./td[2]"), L(kind="structural", q="./td[2]")],
            transform="money", validators=[V(type="number", min=100000, max=100000000)]),
    })

SITES["shop"] = ExtractorSpec(
    site="shop",
    record_selector=[L(kind="css", q="article.product-card"), L(kind="css", q=".product-grid > article")],
    fields={
        "sku": FieldSpec(
            stack=[L(kind="css", q="article", attr="data-sku"), L(kind="xpath", q=".", attr="data-sku"),
                   L(kind="jsonld", q="sku")],
            validators=[V(type="regex", pattern=r"^SKU-\d+$")]),
        "name": FieldSpec(
            stack=[L(kind="css", q="h3.product-title"), L(kind="jsonld", q="name")],
            validators=[V(type="nonempty"), V(type="max_len", max_len=120)]),
        "price": FieldSpec(
            stack=[L(kind="css", q="span.product-price"), L(kind="jsonld", q="offers.price"),
                   L(kind="regex", q=r"\$(\d+\.\d{2})")],
            transform="money", validators=[V(type="number", min=1, max=1000)]),
        "rating": FieldSpec(
            stack=[L(kind="css", q="span.product-rating", attr="data-value"),
                   L(kind="jsonld", q="aggregateRating.ratingValue"), L(kind="css", q="span.product-rating")],
            transform="float", validators=[V(type="number", min=0, max=5)]),
        "stock": FieldSpec(
            stack=[L(kind="css", q="p.product-stock"), L(kind="jsonld", q="offers.availability")],
            validators=[V(type="enum", values=["In stock", "Low stock", "Backorder"])]),
    })

# A link aggregator with no structured data at all. Everything is visible text
# or an href, so there is no schema.org tier to fall back on -- this is the site
# where a redesign has to be repaired rather than absorbed. Note the nav bar and
# footer reuse `.storylink`, `.hnuser` and `.score`, which is realistic and means
# a record-relative locator is the only safe kind.
SITES["hn"] = ExtractorSpec(
    site="hn",
    record_selector=[L(kind="css", q="li.story"), L(kind="css", q="ol.itemlist > li")],
    fields={
        "title": FieldSpec(
            stack=[L(kind="css", q="a.storylink"), L(kind="css", q=".titleline a"),
                   L(kind="structural", q="./*[2]/*[1]/*[1]")],
            validators=[V(type="nonempty"), V(type="max_len", max_len=200)]),
        "points": FieldSpec(
            stack=[L(kind="css", q="span.score"), L(kind="regex", q=r"(\d+)\s+points")],
            transform="int", validators=[V(type="number", min=0, max=100000)]),
        "author": FieldSpec(
            stack=[L(kind="css", q="a.hnuser"), L(kind="text_anchor", q="by", rel="next_sibling_text")],
            validators=[V(type="nonempty"), V(type="max_len", max_len=40)]),
        "comments": FieldSpec(
            stack=[L(kind="css", q="a.commentlink"), L(kind="regex", q=r"(\d+)\s+comments")],
            transform="int", validators=[V(type="number", min=0, max=100000)]),
    })

# A job board carrying JSON-LD, microdata and a definition list. The second
# structured-data site in the corpus, so a `structured_data` repair learned on
# `shop` finally has somewhere to transfer to. Also the only site with a date.
SITES["jobs"] = ExtractorSpec(
    site="jobs",
    record_selector=[L(kind="css", q="li.posting"), L(kind="css", q="ul.posting-list > li"),
                     L(kind="jsonld", q="auto")],
    fields={
        "title": FieldSpec(
            stack=[L(kind="css", q="h2.posting-title"), L(kind="css", q="[itemprop=title]"),
                   L(kind="jsonld", q="title")],
            validators=[V(type="nonempty"), V(type="max_len", max_len=120)]),
        "company": FieldSpec(
            stack=[L(kind="css", q="p.posting-org"), L(kind="css", q="[itemprop=hiringOrganization]"),
                   L(kind="jsonld", q="hiringOrganization.name")],
            validators=[V(type="nonempty"), V(type="max_len", max_len=80)]),
        "location": FieldSpec(
            stack=[L(kind="css", q="dd.fact-location"), L(kind="css", q="[itemprop=jobLocation]"),
                   L(kind="jsonld", q="jobLocation")],
            validators=[V(type="nonempty"), V(type="max_len", max_len=60)]),
        "salary": FieldSpec(
            stack=[L(kind="css", q="dd.fact-salary"),
                   L(kind="text_anchor", q="Salary", rel="next_sibling_text"),
                   L(kind="jsonld", q="baseSalary.value.minValue")],
            transform="money", validators=[V(type="number", min=1000, max=1000000)]),
        "posted": FieldSpec(
            stack=[L(kind="css", q="time", attr="datetime"),
                   L(kind="css", q="[itemprop=datePosted]", attr="datetime"),
                   L(kind="jsonld", q="datePosted"), L(kind="css", q="dd.fact-posted")],
            transform="iso_date", validators=[V(type="regex", pattern=r"^\d{4}-\d{2}-\d{2}$")]),
    })


if __name__ == "__main__":
    for name, spec in SITES.items():
        p = pathlib.Path("eval/sites") / name / "spec.v1.json"
        p.write_text(json.dumps(spec.model_dump(exclude_none=False), indent=2))
        print("wrote", p)
