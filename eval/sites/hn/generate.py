"""Generates the `hn` fixture: a link-aggregator listing with NO structured data.

Deliberately the hostile case in the corpus. `shop` ships JSON-LD and `quotes`
ships microdata, so a redesign there can often be absorbed by a fallback tier
without the repair loop ever being entered. This page offers nothing of the
kind: every field is visible text or an attribute, the metadata line is a single
run of prose with the values embedded in it, and there is a nav bar carrying the
same kind of link markup as the records themselves.

Points and comments are rendered as "123 points" and "45 comments", so the
extractor has to transform rather than read, and a locator that grabs the whole
metadata line still parses to *something* -- which is exactly the plausible
wrong value the monitor has to catch.
"""
import pathlib
import random

random.seed(19)

WORDS_A = ["Rewriting", "Debugging", "Understanding", "Profiling", "Rethinking", "Benchmarking",
           "Deprecating", "Formalising", "Reverse-engineering", "Compiling", "Fuzzing", "Tracing"]
WORDS_B = ["the borrow checker", "a 40-year-old Fortran kernel", "CRDTs", "the Linux scheduler",
           "SQLite's query planner", "WebAssembly GC", "a TLA+ spec", "the CPython interpreter",
           "distributed clocks", "an ARM64 JIT", "column stores", "the DNS root"]
USERS = ["pg", "tptacek", "jgrahamc", "danluu", "aphyr", "antirez", "geohot", "peterthiel",
         "swiftbyte", "hoare_t", "kmett", "rsc"]
SITES = ["acm.org", "usenix.org", "arxiv.org", "lwn.net", "rachelbythebay.com",
         "danluu.com", "blog.regehr.org", "muratbuffalo.blogspot.com"]

items = []
for i in range(30):
    items.append({
        "rank": i + 1,
        "title": f"{WORDS_A[i % len(WORDS_A)]} {WORDS_B[(i * 7) % len(WORDS_B)]}",
        "site": SITES[(i * 3) % len(SITES)],
        "points": random.randint(3, 1240),
        "author": USERS[(i * 5) % len(USERS)],
        "comments": random.randint(0, 480),
        "hours": random.randint(1, 23),
    })

rows = "\n".join(f"""      <li class="story" id="item-{30000 + it['rank'] * 13}">
        <span class="rank">{it['rank']}.</span>
        <div class="story-body">
          <span class="titleline">
            <a class="storylink" href="https://{it['site']}/p/{it['rank']}">{it['title']}</a>
            <span class="sitebit">({it['site']})</span>
          </span>
          <div class="subtext">
            <span class="score">{it['points']} points</span>
            by <a class="hnuser" href="/user?id={it['author']}">{it['author']}</a>
            <span class="age">{it['hours']} hours ago</span>
            | <a class="hide" href="/hide">hide</a>
            | <a class="commentlink" href="/item?id={30000 + it['rank'] * 13}">{it['comments']} comments</a>
          </div>
        </div>
      </li>""" for it in items)

page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Hacker Digest</title></head>
<body>
  <header class="pagetop">
    <a class="storylink" href="/">Hacker Digest</a>
    <nav class="topnav">
      <a class="hnuser" href="/newest">new</a>
      <a class="hnuser" href="/past">past</a>
      <a class="hnuser" href="/ask">ask</a>
    </nav>
  </header>
  <main>
    <ol class="itemlist">
{rows}
    </ol>
    <a class="morelink" href="/?p=2">More</a>
  </main>
  <footer class="pagefoot"><span class="score">0 points</span> &middot; Guidelines</footer>
</body></html>
"""
pathlib.Path(__file__).with_name("page.html").write_text(page)
print("wrote", len(page), "bytes,", len(items), "stories")
