# Baseline agent — B1 one-shot

The comparison baseline: one prompt, the whole broken page, fix the selector. Same model, same three gates, same frozen truth as the Autoheal trajectory.

*Captured from a live run by `eval/trajectories.py`. Prompts and replies are verbatim; every number is what the tools actually returned.*

## Agent instructions (system prompt)

```text
You fix broken web scrapers. You are given the full HTML of a page whose extractor has broken, and the current (broken) locator for each field that stopped working. Return a replacement locator for every field listed.

css and xpath queries are resolved RELATIVE to each record root element. jsonld queries are dotted paths into the page's JSON-LD objects. structural queries are class-free positional xpaths. Your locator must work for EVERY record on the page.

If a field named `__record__` is listed, that is the selector that finds the record root elements themselves. Its query is absolute (document-level), not relative, and it must match exactly one element per record.

Respond with ONLY a JSON object of exactly this shape:
  {"locators": [{"field": <string>, "kind": "css"|"xpath"|"jsonld"|"text_anchor"|"structural"|"regex", "q": <string>, "attr": <string or null>}]}
Include one entry per field you were asked to fix, and nothing else.
```

## Input — the whole page

The prompt carries the complete broken HTML: **13,651 characters**. Autoheal's prompt for the same repair carries ranked candidates instead, and is roughly two orders of magnitude smaller.

```html
RECORD ROOT SELECTOR: css 'div.quote'
FIELDS THAT BROKE:
  - author: currently css 'small.author' (transform: text)
  - text: currently css 'span.text' (transform: text)

NEW PAGE HTML:
<html lang="en">
<head>
	<meta charset="UTF-8">
	<title>Quotes to Scrape</title>
    <link rel="stylesheet" href="/static/bootstrap.min.css">
    <link rel="stylesheet" href="/static/main.css">
    
    
</head>
<body>
    <div class="container">
        <div class="row header-box">
            <div class="col-md-8">
                <h1>
                    <a href="/" style="text-decoration: none">Quotes to Scrape</a>
                </h1>
            </div>
            <div class="col-md-4">
                <p>
                
                    <a href="/login">Login</a>
                
                </p>
            </div>
        </div>
    

<div class="row">
    <div class="col-md-8">

    <div class="quote" itemscope itemtype="http://schema.org/CreativeWork">
        <span class="text compare-at" itemprop="text">Sponsored: “The world as we have created it is a process of our thinking. It cannot be changed without changing our thinking.”</span>
        <span class="text" itemprop="text">
... [12451 more characters]
```

## Reply

```json
{
 "locators": [
  {
   "field": "author",
   "kind": "css",
   "q": "small.author:not(.compare-at)",
   "attr": null
  },
  {
   "field": "text",
   "kind": "css",
   "q": "span.text:not(.compare-at)",
   "attr": null
  }
 ]
}
```

**Tokens:** 4,085 · **wall clock:** 4.9s

## Deterministic scoring of what it returned

| field | kind | query | addressing style | recovers | works on old page |
|---|---|---|---|---|---|
| author | css | `small.author:not(.compare-at)` | exclusion | 1.00 | yes |
| text | css | `span.text:not(.compare-at)` | exclusion | 1.00 | yes |

**Gates:** G1-recovery=pass, G2-regression=pass, G3-clearance=pass

**Outcome:** F1 0.33 -> 1.00 · on the pre-break page 1.00 · healed
