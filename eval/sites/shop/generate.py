"""Generates the `shop` fixture: an SSR product grid carrying both visible
markup and JSON-LD, so 'content deferred into a JSON blob' has a repair path."""
import json, random, pathlib

random.seed(7)
ADJ = ["Nimbus","Harbor","Copper","Vellum","Alder","Quartz","Marlow","Ferrous","Pallas","Bramble","Sable","Onyx"]
NOUN = ["Desk Lamp","Notebook","Kettle","Backpack","Chair","Mug","Speaker","Clock","Stool","Pitcher","Lantern","Tray"]
items = []
for i in range(24):
    items.append({
        "sku": f"SKU-{1000+i*7}",
        "name": f"{ADJ[i % len(ADJ)]} {NOUN[(i*5) % len(NOUN)]}",
        "price": round(random.uniform(8, 240), 2),
        "rating": round(random.uniform(3.0, 5.0), 1),
        "stock": random.choice(["In stock", "Low stock", "In stock", "Backorder"]),
    })

cards = "\n".join(f"""    <article class="product-card" data-sku="{it['sku']}">
      <a class="product-link" href="/p/{it['sku'].lower()}"><img src="/img/{it['sku']}.jpg" alt="{it['name']}"></a>
      <h3 class="product-title">{it['name']}</h3>
      <div class="product-meta">
        <span class="product-price">${it['price']:.2f}</span>
        <span class="product-rating" data-value="{it['rating']}">{it['rating']} / 5</span>
      </div>
      <p class="product-stock">{it['stock']}</p>
    </article>""" for it in items)

ld = [{"@type": "Product", "sku": it["sku"], "name": it["name"],
       "aggregateRating": {"@type": "AggregateRating", "ratingValue": it["rating"]},
       "offers": {"@type": "Offer", "price": f"{it['price']:.2f}", "priceCurrency": "USD",
                  "availability": it["stock"]}} for it in items]

page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Housewares - Catalogue</title>
<script type="application/ld+json">{json.dumps({"@context":"https://schema.org","@graph":ld}, indent=1)}</script>
</head><body>
  <header class="site-header"><a class="brand" href="/">Housewares</a>
    <nav class="site-nav"><a href="/new">New</a><a href="/sale">Sale</a></nav></header>
  <main class="catalogue">
    <h1 class="page-title">All products</h1>
    <section class="product-grid">
{cards}
    </section>
    <nav class="pager"><a class="page-prev" href="/page/0">Previous</a>
      <span class="page-current">1</span><a class="page-next" href="/page/2">Next</a></nav>
  </main>
  <footer class="site-footer"><p>&copy; 2026 Housewares</p></footer>
</body></html>
"""
pathlib.Path(__file__).with_name("page.html").write_text(page)
print("wrote", len(page), "bytes,", len(items), "products")
