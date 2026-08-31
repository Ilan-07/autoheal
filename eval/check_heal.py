"""Drift lockfile for the healing numbers, same contract as `check_baseline`.

The recovery rate is the project's headline claim, so it gets the same treatment
as the B0 floor: if it moves, the build says so. Every number below is fully
deterministic -- no model, no network, and byte-identical under any
PYTHONHASHSEED -- so this is an exact comparison, not a tolerance band.

Run `make heal` and commit results/seed0/heal.json once a change is intended.
"""

from __future__ import annotations

import json
import pathlib
import sys

from eval.heal_eval import _summarise, run_matrix
from autoheal.verify import TAU

COMMITTED = pathlib.Path("results/seed0/heal.json")
WATCHED = ("degraded", "recovery", "quarantine", "mean_cycles", "f1_healed",
           "rank_top1", "rank_top3", "llm_calls", "regressions_on_healthy")


def main() -> int:
    if not COMMITTED.exists():
        print(f"no committed healing results at {COMMITTED}; run `make heal` and commit it")
        return 1
    old = json.loads(COMMITTED.read_text())["summary"]
    fresh = {
        "memory": _summarise(run_matrix(0, memory=True), TAU),
        "no_memory": _summarise(run_matrix(0, memory=False), TAU),
    }

    drift = []
    for arm in ("memory", "no_memory"):
        for k in WATCHED:
            a, b = old.get(arm, {}).get(k), fresh[arm][k]
            if isinstance(a, float) or isinstance(b, float):
                same = a is not None and abs(float(a) - float(b)) < 1e-9
            else:
                same = a == b
            if not same:
                drift.append((arm, k, a, b))

    if not drift:
        print(f"healing results reproduce exactly: recovery {fresh['memory']['recovery']:.2f}, "
              f"{fresh['memory']['degraded']} cases needing repair, matches {COMMITTED}")
        return 0

    print(f"HEALING DRIFT: {len(drift)} differences vs {COMMITTED}\n")
    for arm, k, was, now in drift:
        print(f"  {arm:10} {k:24} {was!r} -> {now!r}")
    print("\nIf intended, run `make heal` and commit results/seed0/heal.json.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
