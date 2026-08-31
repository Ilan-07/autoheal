"""Offline eval: mutate a frozen page, score the static extractor, report.

Zero network, seeded, reproducible. Tonight this measures the B0 baseline (a
static scraper, which by construction never recovers); the healing loop plugs in
at `--agent` once it exists.
"""

from __future__ import annotations

import argparse
import json
import pathlib
from dataclasses import asdict

from autoheal.runtime import extract
from autoheal.spec import ExtractorSpec
from eval import mutators
from autoheal.metrics import score

ROOT = pathlib.Path(__file__).resolve().parent
SITES = ["books", "quotes", "wikitable", "shop", "hn", "jobs"]
BASE_URL = "https://example.test/"

# Single mutations plus two compounds. Compounds matter: a real redesign is
# never one edit, and stacked mutations are what defeat a locator stack.
RECIPES: list[tuple[str, list[str], int]] = [
    ("clean", [], 0),
    ("class_rename", ["class_rename"], 2),
    ("tag_swap", ["tag_swap"], 3),
    ("reparent", ["reparent"], 2),
    ("attr_migration", ["attr_migration"], 2),
    ("decoy_injection", ["decoy_injection"], 2),
    ("content_deferred", ["content_deferred"], 2),
    ("jsonld_drop", ["jsonld_drop"], 2),
    ("record_reorder", ["record_reorder"], 2),
    ("redesign", ["class_rename", "reparent", "attr_migration"], 2),
    ("redesign+decoy", ["class_rename", "reparent", "decoy_injection"], 2),
    # The hard one: kills the primary selector AND the structured-data fallback.
    ("full_rewrite", ["class_rename", "reparent", "jsonld_drop", "tag_swap"], 3),
]


def load(name: str) -> tuple[ExtractorSpec, str]:
    d = ROOT / "sites" / name
    spec = ExtractorSpec(**json.loads((d / "spec.v1.json").read_text()))
    return spec, (d / "page.html").read_text()


def truth(name: str) -> list[dict]:
    return json.loads((ROOT / "sites" / name / "truth.json").read_text())


def run_case(name: str, recipe: str, muts: list[str], severity: int, seed: int) -> dict:
    spec, page = load(name)
    mutated, log = mutators.apply(page, muts, seed=seed, severity=severity) if muts else (page, [])
    run = extract(spec, mutated, base_url=BASE_URL)
    s = score(run.values(), truth(name), spec.field_names())
    # Which fields were served by a fallback tier: the stack absorbing a break
    # with no agent involvement is a real outcome and must be reported as such.
    tier_shift = sorted({
        f for r in run.records for f, res in r.fields.items()
        if res.tier not in (0, None)
    })
    if run.root_tier not in (0, None):
        tier_shift.insert(0, f"<root:tier{run.root_tier}>")
    return {
        "site": name, "recipe": recipe, "seed": seed, "severity": severity,
        "mutations": [asdict(m) for m in log],
        "n_pred": s.n_pred, "n_truth": s.n_truth,
        "macro_f1": round(s.macro_f1, 4), "record_em": round(s.record_em, 4),
        "fill": {k: round(v, 3) for k, v in s.fill.items()},
        "avg_fill": round(sum(s.fill.values()) / len(s.fill), 3) if s.fill else 0.0,
        "field_f1": {k: round(v[2], 3) for k, v in s.fields.items()},
        "silent": s.silent,
        "absorbed_by_fallback": tier_shift,
        "noop": all(m.noop for m in log) if log else False,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--sites", nargs="*", default=SITES)
    ap.add_argument("--out", default="results")
    args = ap.parse_args()

    rows = [run_case(site, r, m, sev, args.seed)
            for site in args.sites for r, m, sev in RECIPES]

    outdir = pathlib.Path(args.out) / f"seed{args.seed}"
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "b0_static.json").write_text(json.dumps(rows, indent=1))

    print(f"\nB0 STATIC BASELINE  (seed={args.seed})  -- no healing, this is the floor\n")
    hdr = f"{'site':11} {'recipe':17} {'recs':>5} {'fill':>6} {'F1':>6} {'recEM':>6}  notes"
    print(hdr); print("-" * len(hdr) + "------")
    for r in rows:
        note = []
        if r["noop"]: note.append("n/a on this page")
        elif r["silent"]: note.append("** SILENT FAILURE **")
        if r["absorbed_by_fallback"]: note.append("stack absorbed: " + ",".join(r["absorbed_by_fallback"]))
        print(f"{r['site']:11} {r['recipe']:17} {r['n_pred']:5} {r['avg_fill']:6.2f} "
              f"{r['macro_f1']:6.2f} {r['record_em']:6.2f}  {'; '.join(note)}")

    live = [r for r in rows if not r["noop"]]
    absorbed = [r for r in live if r["recipe"] != "clean" and r["macro_f1"] >= 0.99 and r["absorbed_by_fallback"]]
    broken = [r for r in live if r["recipe"] != "clean"]
    clean = [r for r in live if r["recipe"] == "clean"]
    silent = [r for r in broken if r["silent"]]
    print(f"\n  clean pages      : mean F1 {sum(c['macro_f1'] for c in clean)/len(clean):.3f}  (must be ~1.00)")
    print(f"  after breakage   : mean F1 {sum(b['macro_f1'] for b in broken)/len(broken):.3f}  over {len(broken)} cases")
    print(f"  silent failures  : {len(silent)}/{len(broken)} cases kept >=90% fill with <90% F1")
    print(f"  absorbed by stack: {len(absorbed)}/{len(broken)} healed at zero cost via a fallback locator")
    print(f"  need repair      : {len([b for b in broken if b['macro_f1'] < 0.99])}/{len(broken)} cases the agent must actually fix")
    print(f"  recovery         : 0.00  (a static scraper cannot repair itself -- by construction)")
    print(f"\n  wrote {outdir/'b0_static.json'}\n")


if __name__ == "__main__":
    main()
