"""The headline eval: does the loop actually heal, and does memory pay for itself?

Same frozen corpus, same seeded mutations, same frozen ground truth as the B0
baseline -- so the recovery numbers are directly comparable to the floor.

Three things are measured that the B0 harness cannot show:

* **recovery** -- of the cases where the static extractor degraded, how many does
  the loop return to F1 >= tau, in how many cycles, and how many end in an honest
  quarantine rather than a wrong answer.
* **the ranker, alone** -- top-1 and top-3 accuracy of `localize` with no model
  involved. `PLAN.md` calls this the make-or-break number, so it is reported
  separately rather than folded into the recovery rate.
* **the memory effect** -- the same matrix run with and without the episode log,
  compared on how many ambiguous decisions needed a model call. Sites are visited
  in a seed-dependent order so the trend is not an artefact of one ordering.

The loop never sees `truth.json`. It is given the extractor's own last-known-good
output as its supervision signal, which is all it would have in production;
frozen truth is used only to score the result afterwards.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
import tempfile

from autoheal.diff import diff
from autoheal.localize import candidates
from autoheal.loop import _repair_roots, heal
from autoheal.patch import apply_patch
from autoheal.memory import Store
from autoheal.metrics import score
from autoheal.perceive import Baseline, perceive
from autoheal.runtime import extract
from autoheal.verify import TAU
from eval import mutators
from eval.harness import BASE_URL, RECIPES, SITES, load, truth

SKIP = {"clean"}


def _prepare(site: str):
    spec, page = load(site)
    clean = extract(spec, page, base_url=BASE_URL)
    return spec, page, clean, Baseline.observe(clean), clean.values()


def ranker_accuracy(spec, page, broken, known_good, fields) -> tuple[int, int, int]:
    """top-1 / top-3 / total, over the fields that actually degraded.

    Measured after the (deterministic, model-free) root repair, because that is
    the state the ranker is actually asked to work in: field candidates are
    generated *inside* record roots, so scoring them on a page whose record
    selector is dead measures the root inducer, not the ranker."""
    staged = spec
    root = _repair_roots(spec, extract(spec, broken, base_url=BASE_URL), broken, known_good, BASE_URL)
    if root is not None:
        staged = apply_patch(spec, [root], created_by="eval", note="staged root repair")
    top1 = top3 = total = 0
    for f in fields:
        if f not in staged.fields:
            continue
        cs = candidates(staged, f, broken, [r.get(f) for r in known_good], old_html=page, base_url=BASE_URL)
        if not cs:
            total += 1
            continue
        total += 1
        ok = [i for i, c in enumerate(cs) if c.recovery >= 0.99 and c.coverage >= 0.99]
        # top-1 and top-3 coincide by construction: recovery dominates the score,
        # so a fully-recovering candidate always sorts to rank 0. Both are
        # reported because PLAN.md asks for top-3; the number that carries the
        # information is whether such a candidate was generated at all.
        top1 += bool(ok and ok[0] == 0)
        top3 += bool(ok and min(ok) < 3)
    return top1, top3, total


def single_locator(spec):
    """The -locator-stack ablation: one selector per field, no fallbacks."""
    out = spec.model_copy(deep=True)
    out.record_selector = out.record_selector[:1]
    for f in out.fields.values():
        f.stack = f.stack[:1]
    return out


def run_matrix(seed: int, *, memory: bool, cross_site_only: bool = False,
               gates: bool = True, use_diff: bool = True, one_locator: bool = False,
               regression_aware: bool = True, known_good_aware: bool = True,
               use_llm: bool = False) -> list[dict]:
    store = Store(tempfile.mkdtemp(prefix="autoheal-eval-")) if memory else None
    rng = random.Random(seed)
    order = list(SITES)
    rng.shuffle(order)  # so a memory trend cannot be an artefact of site order

    rows = []
    prepared = {s: _prepare(s) for s in order}
    for site in order:
        spec, page, clean, baseline, kg = prepared[site]
        if one_locator:
            # Truncate the stack but keep the baseline and known-good values from
            # the full spec: the ablation removes the fallbacks, not the history.
            spec = single_locator(spec)
        for recipe, muts, sev in RECIPES:
            if recipe in SKIP:
                continue
            broken, log = mutators.apply(page, muts, seed=seed, severity=sev)
            if all(m.noop for m in log):
                continue
            base_run = extract(spec, broken, base_url=BASE_URL)
            s0 = score(base_run.values(), truth(site), spec.field_names())
            degraded = s0.macro_f1 < 0.99

            res = heal(spec, broken_html=broken, good_html=page, known_good=kg, baseline=baseline,
                       store=store, base_url=BASE_URL, cross_site_only=cross_site_only,
                       gates=gates, use_diff=use_diff, regression_aware=regression_aware,
                       known_good_aware=known_good_aware, use_llm=use_llm)

            # Score the repaired spec against frozen truth -- the loop never saw it.
            after = extract(res.spec or spec, broken, base_url=BASE_URL)
            s1 = score(after.values(), truth(site), spec.field_names())
            # And on the page that worked *before* the break. This is the number
            # that exposes an overfit: a patch pinned to today's DOM scores 1.00
            # on the broken page and collapses here. Scoring only the broken page
            # made the -verify ablation look almost free, which it is not.
            back = extract(res.spec or spec, page, base_url=BASE_URL)
            s2 = score(back.values(), truth(site), spec.field_names())
            rep = perceive(base_run, baseline, spec)
            t1, t3, tot = ranker_accuracy(spec, page, broken, kg, rep.broken[:6]) if degraded else (0, 0, 0)

            rows.append({
                "site": site, "recipe": recipe, "seed": seed, "memory": memory,
                "degraded": degraded, "fired": res.fired,
                "diff_class": res.diff_class or diff(page, broken).primary,
                "f1_static": round(s0.macro_f1, 4), "f1_healed": round(s1.macro_f1, 4),
                "f1_oldpage": round(s2.macro_f1, 4),
                "healed": res.healed, "quarantined": res.quarantined, "cycles": res.cycles,
                "llm_calls": res.llm_calls, "llm_calls_avoided": res.llm_calls_avoided,
                "used_memory": res.used_memory, "tokens": res.tokens,
                "from_v": res.from_version, "to_v": res.to_version,
                "fields_repaired": res.fields_repaired, "gates": res.gates,
                "rank_top1": t1, "rank_top3": t3, "rank_total": tot,
            })
    return rows


def _summarise(rows: list[dict], tau: float) -> dict:
    deg = [r for r in rows if r["degraded"]]
    healed = [r for r in deg if r["healed"] and r["f1_healed"] >= tau]
    # Accepted as a repair, but still wrong when scored against frozen truth.
    # In any arm with the gates on this must be zero; it is what -verify costs.
    wrong = [r for r in deg if r["healed"] and r["f1_healed"] < tau]
    # Accepted repairs that no longer work on the pre-break page: overfits.
    overfit = [r for r in deg if r["healed"] and r["f1_oldpage"] < tau]
    quar = [r for r in deg if r["quarantined"]]
    clean_cases = [r for r in rows if not r["degraded"]]
    broke_clean = [r for r in clean_cases if r["f1_healed"] < r["f1_static"] - 1e-9]
    t1 = sum(r["rank_top1"] for r in deg)
    t3 = sum(r["rank_top3"] for r in deg)
    tot = sum(r["rank_total"] for r in deg)
    return {
        "cases": len(rows), "degraded": len(deg),
        "recovery": len(healed) / len(deg) if deg else 0.0,
        "quarantine": len(quar) / len(deg) if deg else 0.0,
        "wrong_repairs": len(wrong),
        "overfits": len(overfit),
        "healed_n": len(healed),
        "mean_cycles": sum(r["cycles"] for r in healed) / len(healed) if healed else 0.0,
        "f1_static": sum(r["f1_static"] for r in deg) / len(deg) if deg else 0.0,
        "f1_healed": sum(r["f1_healed"] for r in deg) / len(deg) if deg else 0.0,
        "rank_top1": t1 / tot if tot else 0.0,
        "rank_top3": t3 / tot if tot else 0.0,
        "rank_n": tot,
        "llm_calls": sum(r["llm_calls"] for r in rows),
        "tokens": sum(r["tokens"] for r in rows),
        "llm_avoided": sum(r["llm_calls_avoided"] for r in rows),
        "regressions_on_healthy": len(broke_clean),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--tau", type=float, default=TAU)
    ap.add_argument("--out", default="results")
    ap.add_argument("--cross-site-only", action="store_true",
                    help="recall may only match episodes from a DIFFERENT site (proves transfer)")
    args = ap.parse_args()

    with_mem = run_matrix(args.seed, memory=True, cross_site_only=args.cross_site_only)
    without = run_matrix(args.seed, memory=False)
    a, b = _summarise(with_mem, args.tau), _summarise(without, args.tau)

    outdir = pathlib.Path(args.out) / f"seed{args.seed}"
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "heal.json").write_text(json.dumps(
        {"summary": {"memory": a, "no_memory": b}, "rows": with_mem + without}, indent=1))

    print(f"\nAUTOHEAL END-TO-END  (seed={args.seed}, tau={args.tau})\n")
    hdr = f"{'site':11} {'recipe':17} {'diff class':22} {'F1 static':>9} {'F1 healed':>9} {'cyc':>4}  outcome"
    print(hdr); print("-" * len(hdr))
    for r in with_mem:
        if not r["degraded"]:
            continue
        out = "healed" if r["healed"] else ("QUARANTINED" if r["quarantined"] else "no-op")
        print(f"{r['site']:11} {r['recipe']:17} {r['diff_class']:22} {r['f1_static']:9.2f}"
              f" {r['f1_healed']:9.2f} {r['cycles']:4}  {out} v{r['from_v']}->v{r['to_v']}")

    print(f"\n  cases needing repair : {a['degraded']}")
    print(f"  RECOVERY             : {a['recovery']:.2f}   (B0 static baseline: 0.00 by construction)")
    print(f"  mean F1              : {a['f1_static']:.3f} -> {a['f1_healed']:.3f}")
    print(f"  mean cycles-to-heal  : {a['mean_cycles']:.2f}  (cap 4)")
    print(f"  honest quarantines   : {a['quarantine']:.2f}  (failed loudly instead of writing garbage)")
    print(f"  healthy-case damage  : {a['regressions_on_healthy']}  (cases the loop made worse -- must be 0)")
    print(f"\n  RANKER, NO MODEL AT ALL   top-1 {a['rank_top1']:.2f}   top-3 {a['rank_top3']:.2f}"
          f"   over {a['rank_n']} broken fields")
    print(f"\n  memory effect: ambiguous decisions needing a model call")
    print(f"    with episode memory : {a['llm_calls']:3}   ({a['llm_avoided']} resolved by recall, 0 tokens)")
    print(f"    with memory ablated : {b['llm_calls']:3}")
    if b["llm_calls"]:
        print(f"    reduction           : {1 - a['llm_calls'] / b['llm_calls']:.0%}")
    print(f"    recovery unchanged  : {a['recovery']:.2f} vs {b['recovery']:.2f} ablated")
    print(f"\n  wrote {outdir / 'heal.json'}\n")
    return 0 if a["regressions_on_healthy"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
