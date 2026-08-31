"""B1: the one-shot baseline -- "here's the new HTML, fix the selector".

The comparison `PLAN.md` asks for, and the one Autoheal has to beat. Same model,
same three gates, same frozen ground truth, same scoring. The only thing that
differs is what the model is given: B1 gets the raw broken page, Autoheal gets a
short list of candidates that have each already been executed against every
record on it.

Two deliberate generosities toward B1, so the result cannot be dismissed as a
strawman:

* **It is told which fields broke.** PERCEIVE's output is handed over free. We are
  isolating the repair step, not re-running detection.
* **All broken fields of a page are repaired in one call**, so it pays the cost of
  the HTML once rather than once per field. A careful engineer would batch, so
  the baseline does too.
* **It may repair the record selector too**, under the name `__record__`. The
  first version of this harness did not offer that, and five cases came back
  with F1 1.00, G1 pass, G2 pass and G3 FAIL -- failing purely because the record
  root had fallen to a fallback tier and B1 had never been given the chance to
  fix it, while Autoheal repairs roots as a matter of course. That was our bug,
  not the baseline's, and it understated B1's recovery.

B1 is the one part of this repo whose numbers are NOT reproducible -- a model
decides them. It is therefore excluded from the drift lockfiles, and its results
carry the model name and a timestamp.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import time
import urllib.request

from autoheal.diagnose import DEFAULT_OLLAMA_HOST, LLM_TIMEOUT
from autoheal.localize import evaluate_locator, strategy_of
from autoheal.metrics import score
from autoheal.patch import SpecPatch, apply_patch
from autoheal.perceive import RECORD, Baseline, perceive
from autoheal.runtime import extract
from autoheal.spec import Locator
from autoheal.verify import TAU, verify
from eval import mutators
from eval.harness import BASE_URL, RECIPES, SITES, load, truth

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["locators"],
    "properties": {
        "locators": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["field", "kind", "q", "attr"],
                "properties": {
                    "field": {"type": "string"},
                    "kind": {"type": "string",
                             "enum": ["css", "xpath", "jsonld", "text_anchor", "structural", "regex"]},
                    "q": {"type": "string"},
                    "attr": {"type": ["string", "null"]},
                },
            },
        }
    },
}

SYSTEM = """You fix broken web scrapers. You are given the full HTML of a page whose \
extractor has broken, and the current (broken) locator for each field that stopped \
working. Return a replacement locator for every field listed.

css and xpath queries are resolved RELATIVE to each record root element. jsonld \
queries are dotted paths into the page's JSON-LD objects. structural queries are \
class-free positional xpaths. Your locator must work for EVERY record on the page.

If a field named `__record__` is listed, that is the selector that finds the record \
root elements themselves. Its query is absolute (document-level), not relative, and \
it must match exactly one element per record.

Respond with ONLY a JSON object of exactly this shape:
  {"locators": [{"field": <string>, "kind": "css"|"xpath"|"jsonld"|"text_anchor"|"structural"|"regex", "q": <string>, "attr": <string or null>}]}
Include one entry per field you were asked to fix, and nothing else."""


def ask(model: str, user: str) -> tuple[dict | None, int, float, str]:
    body = json.dumps({
        "model": model, "stream": False, "format": SCHEMA,
        "options": {"temperature": 0, "num_ctx": 131072},
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(f"{DEFAULT_OLLAMA_HOST}/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    t = time.time()
    try:
        raw = json.loads(urllib.request.urlopen(req, timeout=LLM_TIMEOUT).read())
    except Exception as e:
        return None, 0, time.time() - t, f"call failed ({type(e).__name__})"
    tok = int(raw.get("prompt_eval_count") or 0) + int(raw.get("eval_count") or 0)
    try:
        return json.loads(raw["message"]["content"]), tok, time.time() - t, ""
    except Exception:
        return None, tok, time.time() - t, "unparseable JSON"


def run_case(site: str, recipe: str, muts: list[str], sev: int, seed: int, model: str) -> dict | None:
    spec, page = load(site)
    clean = extract(spec, page, base_url=BASE_URL)
    baseline, kg = Baseline.observe(clean), clean.values()
    broken_html, log = mutators.apply(page, muts, seed=seed, severity=sev)
    if all(m.noop for m in log):
        return None

    run0 = extract(spec, broken_html, base_url=BASE_URL)
    s0 = score(run0.values(), truth(site), spec.field_names())
    if s0.macro_f1 >= 0.99:
        return None  # not degraded; nothing for either system to repair

    before = perceive(run0, baseline, spec)
    fields = [f for f in before.broken if f in spec.fields][:6]
    root_sick = RECORD in before.fields and before.fields[RECORD].severity == "critical"
    if not fields and not root_sick:
        return None

    lines = []
    if root_sick:
        rs = spec.record_selector[0]
        lines.append(f"  - __record__: currently {rs.kind} {rs.q!r} (the record root selector)")
    lines += [f"  - {f}: currently {spec.fields[f].stack[0].kind} {spec.fields[f].stack[0].q!r}"
              f" (transform: {spec.fields[f].transform})" for f in fields]
    listing = "\n".join(lines)
    user = (f"RECORD ROOT SELECTOR: {spec.record_selector[0].kind} {spec.record_selector[0].q!r}\n"
            f"FIELDS THAT BROKE:\n{listing}\n\nNEW PAGE HTML:\n{broken_html}")

    reply, tokens, secs, err = ask(model, user)
    patches, chosen = [], []
    if reply:
        for item in (reply.get("locators") or [])[:7]:
            f = item.get("field")
            is_root = f == "__record__"
            if not item.get("q") or (not is_root and f not in spec.fields):
                continue
            loc = Locator(kind=item.get("kind", "css"), q=item["q"], attr=item.get("attr"),
                          note="B1 one-shot")
            if is_root:
                chosen.append({"field": f, "kind": loc.kind, "q": loc.q, "attr": loc.attr,
                               "strategy": strategy_of(loc), "recovery": None,
                               "survives_old": None, "resolves": None})
                patches.append(SpecPatch(field=None, locator=loc, reason="B1 one-shot", strategy="b1"))
                continue
            c = evaluate_locator(spec, f, broken_html, [r.get(f) for r in kg], loc,
                                 old_html=page, base_url=BASE_URL)
            chosen.append({"field": f, "kind": loc.kind, "q": loc.q, "attr": loc.attr,
                           "strategy": strategy_of(loc),
                           "recovery": c.recovery if c else 0.0,
                           "survives_old": bool(c and c.survives_old),
                           "resolves": c is not None})
            patches.append(SpecPatch(field=f, locator=loc, reason="B1 one-shot", strategy="b1"))

    healed = False
    f1_new = f1_old = 0.0
    gates: list[str] = []
    if patches:
        cand = apply_patch(spec, patches, created_by="b1", note="one-shot repair")
        v = verify(cand, broken_html=broken_html, good_html=page, known_good=kg,
                   baseline=baseline, before=before, base_url=BASE_URL, tau=TAU,
                   patched_fields={p.field for p in patches if p.field})
        gates = [f"{g.name}={'pass' if g.passed else 'FAIL'}" for g in v.gates]
        healed = v.passed
        after = extract(cand, broken_html, base_url=BASE_URL)
        back = extract(cand, page, base_url=BASE_URL)
        f1_new = round(score(after.values(), truth(site), spec.field_names()).macro_f1, 4)
        f1_old = round(score(back.values(), truth(site), spec.field_names()).macro_f1, 4)

    return {
        "site": site, "recipe": recipe, "seed": seed, "model": model,
        "fields": fields, "f1_static": round(s0.macro_f1, 4),
        "f1_b1": f1_new, "f1_oldpage": f1_old, "healed": healed, "gates": gates,
        "tokens": tokens, "seconds": round(secs, 1), "error": err,
        "chosen": chosen,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--model", default="gpt-oss:120b-cloud")
    ap.add_argument("--sites", nargs="*", default=SITES)
    ap.add_argument("--limit", type=int, default=0, help="stop after N cases (free-tier friendly)")
    ap.add_argument("--out", default="results")
    args = ap.parse_args()

    outdir = pathlib.Path(args.out) / f"seed{args.seed}"
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / "b1_oneshot.json"

    rows: list[dict] = []
    for site in args.sites:
        for recipe, muts, sev in RECIPES:
            if recipe == "clean" or (args.limit and len(rows) >= args.limit):
                continue
            r = run_case(site, recipe, muts, sev, args.seed, args.model)
            if r is None:
                continue
            rows.append(r)
            flag = "healed" if r["healed"] else ("no-repair" if not r["chosen"] else "FAILED")
            print(f"  {r['site']:11}{r['recipe']:17} {flag:10} F1 {r['f1_static']:.2f}->{r['f1_b1']:.2f}"
                  f"  old-page {r['f1_oldpage']:.2f}  {r['tokens']:6} tok  {r['seconds']:5.1f}s"
                  f"  {r['error']}")
            # Checkpoint every case: a free-tier cutoff must not lose the run.
            path.write_text(json.dumps({"model": args.model, "ts": time.time(), "rows": rows}, indent=1))

    if not rows:
        print("no cases ran")
        return 1

    healed = [r for r in rows if r["healed"] and r["f1_b1"] >= TAU]
    locs = [c for r in rows for c in r["chosen"]]
    overfit = [c for c in locs if c["recovery"] is not None
               and c["recovery"] >= 0.99 and not c["survives_old"]]
    strat: dict[str, int] = {}
    for c in locs:
        strat[c["strategy"]] = strat.get(c["strategy"], 0) + 1

    print(f"\nB1 ONE-SHOT  ({args.model}, seed={args.seed})  -- NOT reproducible; a model decides these\n")
    print(f"  cases                : {len(rows)}")
    print(f"  recovery             : {len(healed) / len(rows):.2f}   ({len(healed)}/{len(rows)} passed all three gates)")
    print(f"  mean F1 after repair : {sum(r['f1_b1'] for r in rows) / len(rows):.3f}")
    print(f"  mean F1 on OLD page  : {sum(r['f1_oldpage'] for r in rows) / len(rows):.3f}   (a repair must not break the page that worked)")
    print(f"  tokens / case        : {sum(r['tokens'] for r in rows) // len(rows)}  (total {sum(r['tokens'] for r in rows)})")
    print(f"  wall clock / case    : {sum(r['seconds'] for r in rows) / len(rows):.1f}s")
    print(f"  locators proposed    : {len(locs)}, of which {len(overfit)} recover fully today but FAIL on the pre-break page")
    print(f"  addressing styles    : {dict(sorted(strat.items(), key=lambda kv: -kv[1]))}")
    print(f"\n  wrote {path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
