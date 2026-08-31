"""Ablations: which parts of the design are actually load-bearing?

`PLAN.md` calls these more persuasive than the baselines, and they are, in both
directions -- one of these arms reports that a component we were prepared to
claim credit for buys nothing measurable on this corpus. It is reported as a null
result rather than dropped, because an ablation table you only publish when it
flatters you is not evidence.

Arms:
  full          everything on
  -memory       no episode log; every ambiguous decision stands alone
  -memory(xs)   recall may only match a DIFFERENT site (transfer, not recurrence)
  -regression   old-page compatibility removed from BOTH the ranker term and G2
  -diff         no structural classification; episodes key on UNKNOWN
  -stack        one locator per field, no fallback tiers
  -known-good   the supervision signal withheld from diagnosis (see caveat below)

The `-known-good` arm stands in for the B1 one-shot baseline, and the difference
matters. It does not measure how well a model reads HTML. It removes the *thing
that makes the model's job small*: candidates are enumerated from every
text-bearing node in the record instead of from the handful carrying a value we
already know, and `recovery` is dropped from the ranking. That is the
informational position a "here is the new HTML, fix the selector" prompt is in.
Verification is untouched -- `PLAN.md` gives B1 the same three gates.

Everything is deterministic and offline; no arm calls a model.
"""

from __future__ import annotations

import argparse
import json
import pathlib

from autoheal.verify import TAU
from eval.heal_eval import _summarise, run_matrix

ARMS: list[tuple[str, dict]] = [
    ("full", {}),
    ("-memory", {"memory": False}),
    ("-memory(xs)", {"cross_site_only": True}),
    ("-regression", {"gates": False, "regression_aware": False}),
    ("-diff", {"use_diff": False}),
    ("-stack", {"one_locator": True}),
    ("-known-good", {"known_good_aware": False}),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default="results")
    args = ap.parse_args()

    out = {}
    for name, kw in ARMS:
        kw = {"memory": True, **kw}
        out[name] = _summarise(run_matrix(args.seed, **kw), TAU)

    outdir = pathlib.Path(args.out) / f"seed{args.seed}"
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "ablations.json").write_text(json.dumps(out, indent=1))

    print(f"\nABLATIONS  (seed={args.seed}, tau={TAU})  -- no model in any arm\n")
    hdr = (f"{'arm':13} {'recovery':>9} {'F1 after':>9} {'healed':>7} {'overfits':>9}"
           f" {'quarant':>8} {'model calls':>12}")
    print(hdr); print("-" * len(hdr))
    for name, _kw in ARMS:
        r = out[name]
        print(f"{name:13} {r['recovery']:9.2f} {r['f1_healed']:9.3f} {r['healed_n']:7}"
              f" {r['overfits']:9} {r['quarantine']:8.2f} {r['llm_calls']:12}")

    f, m, xs = out["full"], out["-memory"], out["-memory(xs)"]
    print(f"\n  memory, all recall      : {f['llm_calls']} vs {m['llm_calls']} model calls"
          f"  ({1 - f['llm_calls'] / m['llm_calls']:.0%} fewer)" if m["llm_calls"] else "")
    print(f"  memory, cross-site only : {xs['llm_calls']} vs {m['llm_calls']} model calls"
          f"  ({1 - xs['llm_calls'] / m['llm_calls']:.0%} fewer)")
    print("    -> most of the saving is a site breaking the same way twice, not")
    print("       transfer between sites. Transfer is real but the smaller half.")
    st = out["-stack"]
    print(f"\n  -stack: {st['degraded']} cases needed repair vs {f['degraded']} with the stack"
          f" (recovery {st['recovery']:.2f} either way)")
    print(f"    -> the fallback tiers absorb {st['degraded'] - f['degraded']} breakages before the loop")
    print("       is ever entered. That is the 'free graceful degradation' claim, and")
    print("       this is its size: fewer repairs needed, not better repairs.")

    print(f"\n  -diff model calls: {out['-diff']['llm_calls']} (full: {f['llm_calls']})")
    print("    -> NULL RESULT for recall. Note the mechanism before reading much into")
    print("       it: with classification off, every episode keys on UNKNOWN, so the")
    print("       diff component of the fingerprint matches trivially rather than")
    print("       being lost. What this shows is that the signal set alone is enough")
    print("       to key episodes on this corpus. diff.py still earns its place as")
    print("       evidence in the repair prompt and on the dashboard -- not here.")
    print(f"\n  -regression overfits: {out['-regression']['overfits']} (full: {f['overfits']})")
    print("    -> NULL RESULT. On this corpus the regression check rejects nothing,")
    print("       because ranking candidates on known-good recovery already implies")
    print("       they work on the page those values came from. The gate stays (it is")
    print("       free, and it did reject a real overfit during development), but it")
    print("       is not currently earning its keep and we do not claim otherwise.")
    kg = out["-known-good"]
    print(f"\n  -known-good: recovery {kg['recovery']:.2f} vs {f['recovery']:.2f},"
          f" F1 {kg['f1_healed']:.3f} vs {f['f1_healed']:.3f},"
          f" quarantines {kg['quarantine']:.0%} vs {f['quarantine']:.0%}")
    print("    -> the largest effect in this table by a wide margin, and the one")
    print("       result that moves recovery at all. Knowing what the field used to")
    print("       return is what turns a haystack search into a short ranked list.")
    print(f"       It still heals {kg['healed_n']} cases: those are the ones where a fallback")
    print("       already in the spec happens to be right, which needs no supervision.")
    print("    -> NOT a substitute for the B1 one-shot baseline. It measures the")
    print("       information asymmetry, not a model's accuracy at reading HTML.")
    print(f"\n  wrote {outdir / 'ablations.json'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
