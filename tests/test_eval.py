import json, os, pathlib
import pytest
from autoheal.runtime import extract
from autoheal.spec import ExtractorSpec
from eval import mutators
from eval.harness import SITES, load, truth
from autoheal.metrics import score

ALL = list(mutators.MUTATORS)


@pytest.mark.parametrize("site", SITES)
def test_ground_truth_holds_independent_invariants(site):
    """The real guard on ground truth.

    Scoring v1 against truth is a tautology -- truth *is* v1's output on the
    clean page, so that assertion is x == x and can never fail. These checks run
    on a different mechanism (raw-text regex, domain invariants like the
    wikitable being a ranked list), so they fail if v1 mis-extracts.
    """
    from eval.verify_truth import verify
    bad = [(n, m) for n, ok, m in verify(site) if not ok]
    assert not bad, f"{site}: {bad}"


@pytest.mark.parametrize("site", SITES)
def test_clean_page_scores_perfectly(site):
    """Tautological on its own (see above); kept only to catch a runtime
    regression that would break extraction without touching truth.json."""
    spec, page = load(site)
    s = score(extract(spec, page, base_url="https://example.test/").values(), truth(site), spec.field_names())
    assert s.macro_f1 == 1.0 and s.record_em == 1.0


@pytest.mark.parametrize("site", SITES)
@pytest.mark.parametrize("mut", ALL)
def test_mutations_are_deterministic(site, mut):
    _, page = load(site)
    a, la = mutators.apply(page, [mut], seed=3, severity=2)
    b, lb = mutators.apply(page, [mut], seed=3, severity=2)
    assert a == b and la[0].detail == lb[0].detail


@pytest.mark.parametrize("site", SITES)
@pytest.mark.parametrize("mut", ALL)
def test_mutations_produce_parseable_html(site, mut):
    from lxml import html as L
    _, page = load(site)
    out, _ = mutators.apply(page, [mut], seed=1, severity=3)
    assert L.fromstring(out) is not None


def test_decoy_is_a_silent_failure_somewhere():
    """The flagship case: fill rate stays high while accuracy collapses."""
    hits = []
    for site in SITES:
        spec, page = load(site)
        out, log = mutators.apply(page, ["decoy_injection"], seed=0, severity=2)
        if log[0].noop:
            continue
        s = score(extract(spec, out, base_url="https://example.test/").values(), truth(site), spec.field_names())
        hits.append(s.silent)
    assert any(hits), "decoy_injection never produced a silent failure"


@pytest.mark.parametrize("site", SITES)
def test_mutations_are_deterministic_across_processes(site):
    """The test that was missing.

    `test_mutations_are_deterministic` compares two calls in one process, where
    set iteration order happens to be stable. It passed while the same seed
    produced different mutations in different processes, because set order
    (PYTHONHASHSEED) fed rng.shuffle. Only a subprocess with a different hash
    seed catches that.
    """
    import subprocess, sys, hashlib
    src = (
        "import pathlib;from eval.mutators import apply;"
        f"out,_=apply(pathlib.Path('eval/sites/{site}/page.html').read_text(),"
        "list(__import__('eval.mutators',fromlist=['x']).MUTATORS),seed=0,severity=2);"
        "print(__import__('hashlib').sha256(out.encode()).hexdigest())"
    )
    digests = set()
    for hs in ("0", "1", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": hs}
        r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True,
                           env=env, cwd=pathlib.Path(__file__).resolve().parent.parent)
        assert r.returncode == 0, r.stderr
        digests.add(r.stdout.strip())
    assert len(digests) == 1, f"{site}: mutation output varies with PYTHONHASHSEED: {digests}"


def test_metrics_penalise_extra_values():
    """Precision must punish plausible-but-absent values, or a decoy scores 1.0."""
    t = [{"a": 1}, {"a": 2}]
    assert score([{"a": 1}, {"a": 2}], t, ["a"]).macro_f1 == 1.0
    assert score([{"a": 1}, {"a": 2}, {"a": 99}], t, ["a"]).macro_f1 < 1.0
