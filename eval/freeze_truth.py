"""Freeze v1 output on the clean page as ground truth. Spot-check, then trust."""
import json, pathlib
from autoheal.spec import ExtractorSpec
from autoheal.runtime import extract
from eval.harness import SITES, load

if __name__ == "__main__":
    for name in SITES:
        spec, page = load(name)
        run = extract(spec, page, base_url="https://example.test/")
        rows = run.values()
        missing = [f for f in spec.fields if any(r[f] is None for r in rows)]
        out = pathlib.Path("eval/sites") / name / "truth.json"
        out.write_text(json.dumps(rows, indent=1, ensure_ascii=False))
        print(f"{name:11} {len(rows):3} records  incomplete_fields={missing or 'none'}  -> {out}")
