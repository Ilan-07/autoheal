"""Generates the `jobs` fixture: a job board with JSON-LD, microdata and a
definition list.

Two gaps in the corpus close here.

First, it is a *second* structured-data site. Cross-site memory transfer was
measured at 19% partly because `books` ships no JSON-LD and so cannot reuse
`shop`'s winning strategy -- there was exactly one site a `structured_data`
episode could ever transfer to. Now there are two.

Second, it is the only site with a **date** field. `t_iso_date` had no corpus
coverage at all, which is how `2024-13-45` survived as a valid-looking value
until the hardening pass. Posted dates are rendered in a human format, so the
transform has to normalise rather than copy.

The salary sits in a `<dd>` whose `<dt>` label is the only thing distinguishing
it from the other two definition rows, which is what makes `text_anchor` worth
having in the stack.
"""
import json
import pathlib
import random

random.seed(23)

ROLES = ["Backend Engineer", "Site Reliability Engineer", "Data Engineer", "Compiler Engineer",
         "Security Analyst", "Platform Engineer", "ML Infrastructure Engineer", "Database Engineer",
         "Embedded Engineer"]
LEVELS = ["", "Senior ", "Staff ", "Principal "]
FIRMS = ["Northwind Systems", "Aurora Labs", "Cobalt Freight", "Meridian Health", "Kestrel Robotics",
         "Lumen Analytics", "Ardent Payments", "Vireo Energy", "Halcyon Media"]
CITIES = ["Berlin, DE", "Austin, TX", "Remote (EU)", "Toronto, ON", "Lisbon, PT",
          "Manchester, UK", "Remote (US)", "Zürich, CH", "Tallinn, EE"]
MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]

items = []
for i in range(18):
    lo = random.randrange(70, 165) * 1000
    items.append({
        "ref": f"JR-{2400 + i * 11}",
        "title": f"{LEVELS[i % len(LEVELS)]}{ROLES[(i * 4) % len(ROLES)]}".strip(),
        "company": FIRMS[(i * 5) % len(FIRMS)],
        "location": CITIES[(i * 7) % len(CITIES)],
        "salary_lo": lo,
        "salary_hi": lo + random.randrange(15, 60) * 1000,
        "month": random.randrange(1, 9),
        "day": random.randrange(1, 28),
    })

cards = "\n".join(f"""      <li class="posting" itemscope itemtype="https://schema.org/JobPosting"
          data-ref="{it['ref']}">
        <h2 class="posting-title" itemprop="title">{it['title']}</h2>
        <p class="posting-org" itemprop="hiringOrganization">{it['company']}</p>
        <dl class="posting-facts">
          <dt class="fact-label">Location</dt>
          <dd class="fact-location" itemprop="jobLocation">{it['location']}</dd>
          <dt class="fact-label">Salary</dt>
          <dd class="fact-salary">${it['salary_lo']:,} &ndash; ${it['salary_hi']:,}</dd>
          <dt class="fact-label">Posted</dt>
          <dd class="fact-posted">
            <time itemprop="datePosted" datetime="2026-{it['month']:02d}-{it['day']:02d}"
              >{it['day']} {MONTHS[it['month'] - 1]} 2026</time>
          </dd>
        </dl>
        <a class="posting-apply" href="/apply/{it['ref'].lower()}">Apply</a>
      </li>""" for it in items)

ld = [{"@type": "JobPosting", "identifier": it["ref"], "title": it["title"],
       "hiringOrganization": {"@type": "Organization", "name": it["company"]},
       "jobLocation": it["location"],
       "datePosted": f"2026-{it['month']:02d}-{it['day']:02d}",
       "baseSalary": {"@type": "MonetaryAmount", "currency": "USD",
                      "value": {"@type": "QuantitativeValue",
                                "minValue": it["salary_lo"], "maxValue": it["salary_hi"]}}}
      for it in items]

page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Openings - Rolodex Jobs</title>
<script type="application/ld+json">{json.dumps({"@context": "https://schema.org", "@graph": ld}, indent=1)}</script>
</head><body>
  <header class="site-header"><a class="brand" href="/">Rolodex Jobs</a>
    <nav class="site-nav"><a href="/companies">Companies</a><a href="/remote">Remote</a></nav></header>
  <main class="board">
    <h1 class="board-title">18 open roles</h1>
    <ul class="posting-list">
{cards}
    </ul>
  </main>
  <footer class="site-footer"><p>&copy; 2026 Rolodex</p></footer>
</body></html>
"""
pathlib.Path(__file__).with_name("page.html").write_text(page)
print("wrote", len(page), "bytes,", len(items), "postings")
