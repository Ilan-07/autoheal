"""Baseline lockfile: fail if the committed B0 numbers no longer reproduce.

The project's whole thesis is that silent drift is the real enemy, so the eval
holds itself to the same standard. Edit a mutator or a v1 spec and the numbers
move; without this, they move quietly and the README slowly becomes fiction.

Run `make eval` to regenerate results/seed0/b0_static.json once the change is
intended -- the diff then shows up in review as a deliberate act.
"""

from __future__ import annotations

import json
import pathlib
import sys

from eval.harness import RECIPES, SITES, run_case

COMMITTED = pathlib.Path("results/seed0/b0_static.json")
WATCHED = ("macro_f1", "record_em", "n_pred", "silent", "noop", "absorbed_by_fallback")


def main() -> int:
    if not COMMITTED.exists():
        print(f"no committed baseline at {COMMITTED}; run `make eval` and commit it")
        return 1

    old = {(r["site"], r["recipe"]): r for r in json.loads(COMMITTED.read_text())}
    fresh = [run_case(s, r, m, sev, 0) for s in SITES for r, m, sev in RECIPES]

    drift = []
    for row in fresh:
        key = (row["site"], row["recipe"])
        if key not in old:
            drift.append((key, "case", "absent", "new case"))
            continue
        for f in WATCHED:
            if row[f] != old[key][f]:
                drift.append((key, f, old[key][f], row[f]))
    for key in set(old) - {(r["site"], r["recipe"]) for r in fresh}:
        drift.append((key, "case", "present", "removed"))

    if not drift:
        print(f"baseline reproduces exactly: {len(fresh)} cases match {COMMITTED}")
        return 0

    print(f"BASELINE DRIFT: {len(drift)} differences vs {COMMITTED}\n")
    for (site, recipe), field, was, now in drift:
        print(f"  {site:11} {recipe:17} {field:22} {was!r} -> {now!r}")
    print("\nIf intended, run `make eval` and commit results/seed0/b0_static.json.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
