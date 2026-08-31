"""Tests for the code that computes the published numbers.

`autoheal/` was at 93% covered while `eval/` sat at 34%, with `heal_eval`,
`ablations`, `perceive_eval` and the two drift lockfiles at zero. They run
end-to-end in `make all`, but nothing checked their arithmetic -- and the
arithmetic *is* the claim. A wrong denominator in `_summarise` would move the
headline recovery rate with no test anywhere going red.

The drift-checker tests matter for the same reason the false-alarm metric does:
a lockfile that has only ever passed is not evidence that it can fail.
"""

import json
import pathlib
import subprocess
import sys

import pytest

from autoheal.verify import TAU
from eval.heal_eval import _summarise


def row(**kw):
    base = dict(site="s", recipe="r", seed=0, memory=True, degraded=True, fired=True,
                diff_class="X", f1_static=0.0, f1_healed=1.0, f1_oldpage=1.0,
                healed=True, quarantined=False, cycles=1, llm_calls=0,
                llm_calls_avoided=0, used_memory=False, tokens=0, from_v=1, to_v=2,
                fields_repaired=[], gates=[], rank_top1=0, rank_top3=0, rank_total=0)
    base.update(kw)
    return base


def test_summarise_counts_recovery_only_for_cases_that_cleared_tau():
    rows = [
        row(healed=True, f1_healed=1.00),   # counts
        row(healed=True, f1_healed=0.95),   # counts
        row(healed=True, f1_healed=0.50),   # healed flag but below tau -> NOT recovery
        row(healed=False, quarantined=True, f1_healed=0.30),
    ]
    s = _summarise(rows, TAU)
    assert s["degraded"] == 4
    assert s["recovery"] == pytest.approx(2 / 4)
    assert s["wrong_repairs"] == 1, "a patch accepted below tau must be counted, not ignored"
    assert s["quarantine"] == pytest.approx(1 / 4)


def test_summarise_ignores_non_degraded_cases_in_the_recovery_denominator():
    """Cases the static extractor handled fine are not repairs. Including them
    would inflate recovery by counting work that never needed doing."""
    rows = [row(degraded=True, healed=True, f1_healed=1.0),
            row(degraded=False, healed=False, f1_healed=1.0),
            row(degraded=False, healed=False, f1_healed=1.0)]
    s = _summarise(rows, TAU)
    assert s["degraded"] == 1 and s["recovery"] == 1.0
    assert s["cases"] == 3


def test_summarise_flags_damage_to_healthy_cases():
    rows = [row(degraded=False, f1_static=1.0, f1_healed=0.4)]
    assert _summarise(rows, TAU)["regressions_on_healthy"] == 1
    rows = [row(degraded=False, f1_static=1.0, f1_healed=1.0)]
    assert _summarise(rows, TAU)["regressions_on_healthy"] == 0


def test_summarise_counts_an_overfit_by_the_old_page_not_the_broken_one():
    """The distinction the -regression ablation turns on: a patch can score 1.00
    on the page that broke and still be an overfit."""
    rows = [row(healed=True, f1_healed=1.0, f1_oldpage=0.2)]
    s = _summarise(rows, TAU)
    assert s["overfits"] == 1 and s["wrong_repairs"] == 0


def test_summarise_means_use_the_right_denominators():
    rows = [row(healed=True, f1_healed=1.0, cycles=1),
            row(healed=True, f1_healed=0.8, cycles=3),
            row(healed=False, quarantined=True, f1_healed=0.0, cycles=4)]
    s = _summarise(rows, TAU)
    # mean F1 is over all degraded cases...
    assert s["f1_healed"] == pytest.approx((1.0 + 0.8 + 0.0) / 3)
    # ...but mean cycles is over the ones that actually recovered.
    assert s["mean_cycles"] == pytest.approx(1.0)


def test_summarise_ranker_rates_use_field_counts_not_case_counts():
    rows = [row(rank_top1=2, rank_top3=3, rank_total=4),
            row(rank_top1=1, rank_top3=1, rank_total=2)]
    s = _summarise(rows, TAU)
    assert s["rank_n"] == 6
    assert s["rank_top1"] == pytest.approx(3 / 6)
    assert s["rank_top3"] == pytest.approx(4 / 6)


def test_summarise_survives_an_empty_matrix():
    s = _summarise([], TAU)
    assert s["recovery"] == 0.0 and s["degraded"] == 0 and s["rank_top1"] == 0.0


# --- the drift lockfiles must be able to fail --------------------------------

def _run(mod, cwd):
    return subprocess.run([sys.executable, "-m", mod], capture_output=True, text=True, cwd=cwd)


@pytest.mark.parametrize("mod,fixture,mangle", [
    ("eval.check_baseline", "results/seed0/b0_static.json",
     lambda d: [{**r, "macro_f1": 0.123} for r in d]),
    ("eval.check_heal", "results/seed0/heal.json",
     lambda d: {**d, "summary": {**d["summary"],
                                 "memory": {**d["summary"]["memory"], "recovery": 0.123}}}),
])
def test_drift_checker_actually_detects_drift(tmp_path, mod, fixture, mangle):
    """A lockfile that has only ever passed proves nothing. Corrupt the committed
    numbers in a scratch copy and the checker must exit non-zero."""
    repo = pathlib.Path(__file__).resolve().parent.parent
    work = tmp_path / "repo"
    work.mkdir()
    for name in ("autoheal", "eval", "results", "pyproject.toml"):
        src = repo / name
        (subprocess.run(["cp", "-R", str(src), str(work / name)], check=True))

    target = work / fixture
    target.write_text(json.dumps(mangle(json.loads(target.read_text()))))
    bad = _run(mod, work)
    assert bad.returncode != 0, f"{mod} passed on deliberately corrupted numbers"
    assert "DRIFT" in (bad.stdout + bad.stderr).upper()


def test_drift_checker_passes_on_the_committed_numbers():
    repo = pathlib.Path(__file__).resolve().parent.parent
    for mod in ("eval.check_baseline", "eval.check_heal"):
        r = _run(mod, repo)
        assert r.returncode == 0, f"{mod} failed on committed numbers:\n{r.stdout}{r.stderr}"


# --- the corpus must match the scripts that generate it ---------------------

@pytest.mark.parametrize("site", ["books", "quotes", "wikitable", "shop"])
def test_committed_specs_match_the_authoring_script(site):
    """`author_specs.py` and `eval/sites/*/spec.v1.json` can drift apart: edit one
    and the other silently keeps the old definition, so `make truth` would
    regenerate a corpus that no longer matches the published numbers."""
    from autoheal.spec import ExtractorSpec
    from eval.author_specs import SITES as AUTHORED
    committed = ExtractorSpec(**json.loads(
        pathlib.Path(f"eval/sites/{site}/spec.v1.json").read_text()))
    assert committed.model_dump() == AUTHORED[site].model_dump()


@pytest.mark.parametrize("site", ["books", "quotes", "wikitable", "shop"])
def test_committed_truth_is_still_what_v1_produces(site):
    """What `freeze_truth.py` would write must equal what is committed. This is
    the reproducibility of the corpus itself, distinct from `verify_truth`,
    which checks that the corpus is *correct* by an independent mechanism."""
    from autoheal.runtime import extract
    from eval.harness import BASE_URL, load, truth
    spec, page = load(site)
    assert extract(spec, page, base_url=BASE_URL).values() == truth(site)


# --- the ablation arms must actually ablate ---------------------------------

def test_every_ablation_arm_is_a_real_run_matrix_option():
    """A typo'd kwarg would raise, but an arm whose kwargs match the defaults
    would silently duplicate `full` and be reported as a null result."""
    import inspect
    from eval.ablations import ARMS
    from eval.heal_eval import run_matrix
    sig = inspect.signature(run_matrix)
    defaults = {k: v.default for k, v in sig.parameters.items()
                if v.default is not inspect.Parameter.empty}
    names = [n for n, _ in ARMS]
    assert names[0] == "full" and len(set(names)) == len(names)
    for name, kw in ARMS[1:]:
        assert kw, f"{name} ablates nothing"
        for k, v in kw.items():
            assert k in sig.parameters, f"{name}: {k} is not a run_matrix parameter"
            assert v != defaults.get(k), f"{name}: {k}={v!r} is already the default"


# --- perceive_eval and b1_oneshot -------------------------------------------

def test_perceive_eval_case_reports_control_and_degraded_correctly():
    from eval.perceive_eval import run_case
    clean = run_case("shop", "clean", [], 0, 0)
    assert clean["control"] and not clean["degraded"] and not clean["fired"]
    decoy = run_case("shop", "decoy_injection", ["decoy_injection"], 2, 0)
    assert decoy["degraded"] and decoy["fired"] and not decoy["control"]


def _b1(monkeypatch, reply):
    import eval.b1_oneshot as b1
    monkeypatch.setattr(b1, "ask", lambda model, user: (reply, 1234, 1.5, ""))
    return b1.run_case("shop", "decoy_injection", ["decoy_injection"], 2, 0, "fake-model")


def test_b1_applies_a_valid_reply_and_runs_the_same_gates(monkeypatch):
    r = _b1(monkeypatch, {"locators": [
        {"field": "price", "kind": "jsonld", "q": "offers.price", "attr": None}]})
    assert r["tokens"] == 1234 and r["chosen"]
    assert r["chosen"][0]["strategy"] == "structured_data"
    assert len(r["gates"]) == 3


def test_b1_may_repair_the_record_selector(monkeypatch):
    """The asymmetry that made the first B1 run score 0.10: without this the
    baseline could never clear G3 on a page whose root selector had moved."""
    r = _b1(monkeypatch, {"locators": [
        {"field": "__record__", "kind": "css", "q": "article.product-card", "attr": None}]})
    assert any(c["field"] == "__record__" for c in r["chosen"])


@pytest.mark.parametrize("reply", [
    None,
    {"locators": []},
    {"locators": [{"field": "no_such_field", "kind": "css", "q": ".x", "attr": None}]},
    {"locators": [{"field": "price", "kind": "css", "q": "", "attr": None}]},
    {"wrong_key": 1},
])
def test_b1_discards_unusable_replies_without_crashing(monkeypatch, reply):
    r = _b1(monkeypatch, reply)
    assert r["chosen"] == [] and r["gates"] == [] and not r["healed"]
