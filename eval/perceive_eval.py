"""Does the monitor actually work? Detection rate and false-alarm rate.

A breakage detector is only as good as its quiet days. This measures both ends
against the same frozen corpus the extraction baseline uses:

* **detection** -- of the cases where the extractor really did degrade (F1 < 0.99
  against frozen truth), how many did PERCEIVE fire on?
* **false alarms** -- on the unmutated page and on `content_churn` (new content,
  identical structure), how often did it fire anyway?
* **early warning** -- cases where the locator stack absorbed the break and F1
  stayed at 1.00. Firing here is not a false alarm; it is the leading indicator
  the whole locator-stack design exists to produce.
"""

from __future__ import annotations

import argparse
import json
import pathlib

from autoheal.perceive import Baseline, perceive
from autoheal.runtime import extract
from eval import mutators
from eval.harness import BASE_URL, RECIPES, SITES, load, truth
from autoheal.metrics import score

CONTROLS: list[tuple[str, list[str], int]] = [("content_churn", ["content_churn"], 2)]


def run_case(site: str, recipe: str, muts: list[str], severity: int, seed: int) -> dict:
    spec, page = load(site)
    baseline = Baseline.observe(extract(spec, page, base_url=BASE_URL))
    mutated, log = mutators.apply(page, muts, seed=seed, severity=severity) if muts else (page, [])
    run = extract(spec, mutated, base_url=BASE_URL)
    rep = perceive(run, baseline, spec)
    s = score(run.values(), truth(site), spec.field_names())

    noop = bool(log) and all(m.noop for m in log)
    control = recipe in {"clean"} | {c[0] for c in CONTROLS}
    degraded = (not control) and (not noop) and s.macro_f1 < 0.99
    return {
        "site": site, "recipe": recipe, "seed": seed, "noop": noop, "control": control,
        "macro_f1": round(s.macro_f1, 4), "silent": s.silent, "degraded": degraded,
        "fired": rep.fired, "flagged": bool(rep.broken),
        "score": round(max((h.score for h in rep.fields.values()), default=0.0), 3),
        "broken_fields": rep.broken,
        "signals": rep.signal_names,
        "evidence": rep.evidence()[:4],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--sites", nargs="*", default=SITES)
    ap.add_argument("--out", default="results")
    args = ap.parse_args()

    rows = [run_case(s, r, m, sev, args.seed)
            for s in args.sites for r, m, sev in RECIPES + CONTROLS]

    outdir = pathlib.Path(args.out) / f"seed{args.seed}"
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "perceive.json").write_text(json.dumps(rows, indent=1))

    print(f"\nPERCEIVE  (seed={args.seed})  -- detection vs false alarms\n")
    hdr = f"{'site':11} {'recipe':17} {'F1':>5} {'score':>6} {'verdict':9}  fields / why"
    print(hdr); print("-" * len(hdr) + "----")
    for r in rows:
        verdict = "FIRED" if r["fired"] else ("warn" if r["flagged"] else "quiet")
        if r["noop"]:
            verdict = "n/a"
        why = ",".join(r["broken_fields"][:3]) or "-"
        if r["control"] and (r["fired"] or r["flagged"]):
            why += "   << FALSE ALARM"
        if r["degraded"] and not r["fired"]:
            why += "   << MISS"
        print(f"{r['site']:11} {r['recipe']:17} {r['macro_f1']:5.2f} {r['score']:6.2f} {verdict:9}  {why}")

    live = [r for r in rows if not r["noop"]]
    degraded = [r for r in live if r["degraded"]]
    controls = [r for r in live if r["control"]]
    silent = [r for r in degraded if r["silent"]]
    absorbed = [r for r in live if not r["control"] and not r["degraded"]]

    det = sum(r["fired"] for r in degraded) / len(degraded) if degraded else 0.0
    sil = sum(r["fired"] for r in silent) / len(silent) if silent else 0.0
    fpr = sum(r["fired"] for r in controls) / len(controls) if controls else 0.0
    fpr_warn = sum(r["flagged"] for r in controls) / len(controls) if controls else 0.0
    early = sum(r["fired"] or r["flagged"] for r in absorbed) / len(absorbed) if absorbed else 0.0

    print(f"\n  detection rate     : {det:.2f}  ({sum(r['fired'] for r in degraded)}/{len(degraded)} genuinely degraded cases fired)")
    print(f"  silent failures    : {sil:.2f}  ({sum(r['fired'] for r in silent)}/{len(silent)} high-fill/low-F1 cases caught)")
    print(f"  FALSE ALARM (fire) : {fpr:.2f}  ({sum(r['fired'] for r in controls)}/{len(controls)} clean + content-churn pages)")
    print(f"  false alarm (warn) : {fpr_warn:.2f}  (same pages, counting warnings too)")
    print(f"  early warning      : {early:.2f}  ({sum(r['fired'] or r['flagged'] for r in absorbed)}/{len(absorbed)} breaks the stack absorbed were still noticed)")
    print(f"\n  wrote {outdir/'perceive.json'}\n")

    missed = [r for r in degraded if not r["fired"]]
    return 1 if (fpr > 0 or missed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
