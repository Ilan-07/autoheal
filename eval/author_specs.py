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

if __name__ == "__main__":
    for name, spec in SITES.items():
        p = pathlib.Path("eval/sites") / name / "spec.v1.json"
        p.write_text(json.dumps(spec.model_dump(exclude_none=False), indent=2))
        print("wrote", p)
