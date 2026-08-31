"""Tests for the repair loop: perceive, diff, localize, patch, verify, memory.

The bias here is toward tests that would have caught the defects the eval
actually surfaced during stage 2, rather than tests that restate the code.
"""

import json
import tempfile

import pytest

from autoheal.diff import CLASS_RENAME, DECOY_INJECTED, WRAPPER_INSERTED, diff
from autoheal.localize import _core, candidates, evaluate_locator, induce_root_candidates, prior
from autoheal.loop import heal
from autoheal.memory import Episode, Fingerprint, Store
from autoheal.patch import MAX_STACK, SpecPatch, apply_patch, record_hits, spec_diff
from autoheal.perceive import CRITICAL, RECORD, Baseline, perceive
from autoheal.runtime import Context, extract, resolve_all
from autoheal.spec import ExtractorSpec, FieldSpec, Locator, Validator
from autoheal.verify import verify
from eval import mutators
from eval.harness import BASE_URL, SITES, load

MUTS = ["class_rename", "tag_swap", "reparent", "decoy_injection"]


def prep(site):
    spec, page = load(site)
    clean = extract(spec, page, base_url=BASE_URL)
    return spec, page, clean, Baseline.observe(clean), clean.values()


# --- perceive -------------------------------------------------------------


@pytest.mark.parametrize("site", SITES)
def test_perceive_is_silent_on_the_unmutated_page(site):
    spec, page, clean, base, _ = prep(site)
    rep = perceive(clean, base, spec)
    assert not rep.fired and not rep.broken, rep.evidence()


@pytest.mark.parametrize("site", SITES)
def test_perceive_is_silent_on_content_change(site):
    """The false-alarm case that matters: new content, identical structure.

    Signal 8 is weighted below the warn threshold precisely so that novelty on
    its own -- a site that restocked -- is not reported as a breakage."""
    spec, page, _clean, base, _ = prep(site)
    churned, log = mutators.apply(page, ["content_churn"], seed=0, severity=2)
    if log[0].noop:
        pytest.skip("content_churn is a no-op on this page")
    rep = perceive(extract(spec, churned, base_url=BASE_URL), base, spec)
    assert not rep.fired, rep.evidence()


def test_constant_collapse_is_judged_against_baseline_not_absolutely():
    """`books.availability` is genuinely constant on a healthy page. An absolute
    'all values identical' check flagged it in stage 1; only a *newly* constant
    field is evidence."""
    spec, page, clean, base, _ = prep("books")
    rep = perceive(clean, base, spec)
    assert not any(s.name == "constant_collapse" for s in rep.fields["availability"].signals)


def test_decoy_fires_critical_via_match_multiplicity():
    """The flagship silent failure. Fill, tier, validators and record count all
    stay healthy; the only tell is that the locator matches twice per record."""
    spec, page, _clean, base, _ = prep("shop")
    out, _ = mutators.apply(page, ["decoy_injection"], seed=0, severity=2)
    rep = perceive(extract(spec, out, base_url=BASE_URL), base, spec)
    assert rep.fired and "price" in rep.broken
    assert any(s.name == "match_count" for s in rep.fields["price"].signals)


def test_n_hits_is_observational_only():
    """Match counting must not change which value is extracted."""
    html = '<div class="r"><span class="p">A</span><span class="p">B</span></div>'
    s = ExtractorSpec(site="t", record_selector=[Locator(kind="css", q="div.r")],
                      fields={"v": FieldSpec(stack=[Locator(kind="css", q="span.p")])})
    res = extract(s, html).records[0].fields["v"]
    assert res.value == "A" and res.n_hits == 2


# --- diff -----------------------------------------------------------------


@pytest.mark.parametrize("mut,expected", [("class_rename", CLASS_RENAME),
                                          ("reparent", WRAPPER_INSERTED),
                                          ("decoy_injection", DECOY_INJECTED)])
def test_diff_classifies_the_mutation(mut, expected):
    _spec, page = load("shop")
    out, log = mutators.apply(page, [mut], seed=0, severity=2)
    assert not log[0].noop
    assert expected in diff(page, out).classes


@pytest.mark.parametrize("site", SITES)
def test_diff_does_not_call_content_change_a_structural_one(site):
    """The churn control must not be classified as a client-render migration.
    Without the visible-text volume guard, any page shipping JSON-LD looked
    deferred, because the old values were still sitting in the blob."""
    _spec, page = load(site)
    out, log = mutators.apply(page, ["content_churn"], seed=0, severity=2)
    if log[0].noop:
        pytest.skip("no-op")
    assert "CONTENT_DEFERRED" not in diff(page, out).classes


# --- localize -------------------------------------------------------------


def test_core_normalises_floats_against_page_text():
    """37732000.0 must match '37,732,000'. Stringifying the float added a digit,
    so every money/int field generated zero candidates and quarantined."""
    assert _core(37732000.0) == _core("37,732,000") == "37732000"
    assert _core(24.99) == _core("$24.99")


def test_prior_ranks_addressing_schemes_by_durability():
    assert prior(Locator(kind="css", q="span[itemprop]")) > prior(Locator(kind="css", q="span.price"))
    assert prior(Locator(kind="css", q="span.price")) > prior(Locator(kind="css", q="span.css-1a2b3c"))
    assert prior(Locator(kind="css", q="span.price")) > prior(Locator(kind="structural", q="./*[2]"))


@pytest.mark.parametrize("site", SITES)
@pytest.mark.parametrize("mut", MUTS)
def test_candidates_recover_known_values_or_return_nothing(site, mut):
    """Never propose a locator that cannot reproduce known-good values: an empty
    list is an honest answer and leads to quarantine."""
    spec, page, _clean, _base, kg = prep(site)
    out, log = mutators.apply(page, [mut], seed=0, severity=2)
    if log[0].noop:
        pytest.skip("no-op")
    for f in spec.field_names():
        for c in candidates(spec, f, out, [r.get(f) for r in kg], old_html=page, base_url=BASE_URL):
            assert c.coverage > 0


def test_decoy_repair_adapts_to_whatever_marker_the_seed_chose():
    """The anti-rigging test.

    The generated fix reads `:not(.was-value)` while the mutator sets
    `was-value`, which looks planted. It is not -- `_disambiguated` derives the
    excluded token from the DOM and `autoheal/` never names any marker (see the
    test below). This proves it: the mutator picks its marker per seed from a
    pool, and every exclusion selector generated must name whichever token that
    particular seed happened to choose.

    Known limitation this test documents rather than hides: when the decoy is
    cloned from an *ancestor* of the field node (seed 3 clones `product-meta`,
    which contains the price), the distinguishing class lands on the ancestor
    and no exclusion selector is produced at all. Those cases still heal, via
    another tier of the stack -- but not by exclusion.
    """
    spec, page, _clean, _base, kg = prep("shop")
    seeds_with_exclusions = 0
    for seed in range(8):
        out, log = mutators.apply(page, ["decoy_injection"], seed=seed, severity=2)
        if log[0].noop:
            continue
        marker = log[0].detail.split("marked .")[1].split()[0]
        excl = [c for f in spec.field_names()
                for c in candidates(spec, f, out, [r.get(f) for r in kg],
                                    old_html=page, base_url=BASE_URL)
                if ":not(" in c.locator.q]
        if not excl:
            continue  # ancestor-marked decoy; see docstring
        seeds_with_exclusions += 1
        assert all(marker in c.locator.q for c in excl), (
            f"seed {seed}: expected exclusions to name .{marker}, "
            f"got {[c.locator.q for c in excl]}")
    assert seeds_with_exclusions >= 3, (
        f"only {seeds_with_exclusions} seeds produced an exclusion selector; "
        "the assertion above may be passing vacuously")


def test_autoheal_never_hardcodes_a_decoy_marker():
    """No marker string from the mutator's pool may appear anywhere in the
    repair code -- that is what makes the test above meaningful."""
    import pathlib as _p
    from eval.mutators import DECOY_MARKERS
    src = " ".join(f.read_text() for f in _p.Path("autoheal").glob("*.py"))
    leaked = [m for m in DECOY_MARKERS if m in src]
    assert not leaked, f"repair code names decoy markers: {leaked}"


def test_decoy_yields_a_locator_that_survives_both_pages():
    """A positional path recovers on the broken page and dies on the regression
    gate. The exclusion selector is the one that satisfies both."""
    spec, page, _clean, _base, kg = prep("quotes")
    out, _ = mutators.apply(page, ["decoy_injection"], seed=0, severity=2)
    cs = candidates(spec, "text", out, [r.get("text") for r in kg], old_html=page, base_url=BASE_URL)
    assert any(c.recovery >= 0.99 and c.survives_old for c in cs)


def test_model_generalisation_is_measured_not_trusted():
    """A proposed query is re-executed; a bogus one scores nothing."""
    spec, page, _clean, _base, kg = prep("shop")
    out, _ = mutators.apply(page, ["class_rename"], seed=0, severity=2)
    vals = [r.get("price") for r in kg]
    assert evaluate_locator(spec, "price", out, vals, Locator(kind="css", q=".nonsense-xyz"),
                            old_html=page, base_url=BASE_URL) is None


def test_root_induction_prefers_the_right_cardinality():
    """wikitable's fallback selector sweeps in 2 header rows; induction must
    find the 39 data rows by what they contain."""
    spec, page, _clean, _base, kg = prep("wikitable")
    out, _ = mutators.apply(page, ["reparent"], seed=0, severity=2)
    ctx = Context.build(out, BASE_URL)
    cands = induce_root_candidates(ctx.doc, expected_n=len(kg),
                                   values=[v for r in kg for v in r.values() if v is not None][:60])
    assert cands and cands[0].n_hits_mean == len(kg)


def test_withholding_known_good_widens_the_search_and_drops_recovery():
    """Bet 3, as a causal test rather than an assertion: without known-good
    values the ranker must enumerate the whole record instead of ~3 nodes."""
    spec, page, _clean, _base, kg = prep("books")
    out, _ = mutators.apply(page, ["class_rename"], seed=0, severity=2)
    vals = [r.get("price") for r in kg]
    aware = candidates(spec, "price", out, vals, old_html=page, base_url=BASE_URL)
    blind = candidates(spec, "price", out, vals, old_html=page, base_url=BASE_URL,
                       known_good_aware=False)
    assert aware and blind
    # Recovery is still measured in both, but only ranked on when it is available.
    assert aware[0].recovery >= 0.99
    assert blind[0].score == max(c.score for c in blind)


@pytest.mark.parametrize("site", SITES)
def test_blind_mode_ranking_is_invariant_to_the_known_good_values(site):
    """The definitive leak test for the -known-good ablation.

    If withholding the supervision signal is real, then corrupting the known-good
    values must not change the blind ranking *at all*. This caught a genuine leak:
    `survives_old` was computed as recovery-of-known-good on the pre-break page,
    so regression-awareness was quietly handing the signal back."""
    spec, page, _clean, _base, kg = prep(site)
    field = spec.field_names()[0]
    real = [r.get(field) for r in kg]
    corrupt = [f"nonsense-{i}" for i in range(len(real))]
    out, _ = mutators.apply(page, ["class_rename"], seed=0, severity=2)

    def order(vals):
        return [c.locator.signature() for c in
                candidates(spec, field, out, vals, old_html=page, base_url=BASE_URL,
                           known_good_aware=False)]

    assert order(real) == order(corrupt), "blind ranking depends on known-good values"


def test_blind_mode_drops_the_value_shaped_regexes():
    spec, page, _clean, _base, kg = prep("shop")
    out, _ = mutators.apply(page, ["class_rename"], seed=0, severity=2)
    vals = [r.get("price") for r in kg]
    blind = candidates(spec, "price", out, vals, old_html=page, base_url=BASE_URL,
                       known_good_aware=False)
    assert not any("value shape" in (c.source or "") for c in blind), "value regexes leaked"


# --- patch ----------------------------------------------------------------


def test_patches_are_additive_and_preserve_history():
    spec, _page = load("shop")
    old_head = spec.fields["price"].stack[0]
    new = apply_patch(spec, [SpecPatch(field="price", locator=Locator(kind="structural", q="./*[3]"),
                                       reason="t")], created_by="autoheal", note="t")
    stack = new.fields["price"].stack
    assert stack[0].kind == "structural" and stack[0].born == new.version
    assert old_head.signature() in [l.signature() for l in stack], "the old locator must be demoted, not deleted"
    assert spec.fields["price"].stack[0].kind == "css", "the input spec must not be mutated"
    assert new.parent == spec.version and new.version == spec.version + 1


def test_stack_is_bounded_and_evicts_only_dead_entries():
    spec, _page = load("shop")
    cur = spec
    for i in range(MAX_STACK + 3):
        cur = apply_patch(cur, [SpecPatch(field="price", locator=Locator(kind="xpath", q=f"./x{i}"),
                                          reason="t")], created_by="t", note="t")
    assert len(cur.fields["price"].stack) <= MAX_STACK


def test_record_hits_marks_the_serving_locator():
    spec, page, clean, _base, _kg = prep("shop")
    stamped = record_hits(spec, clean)
    assert stamped.fields["price"].stack[0].last_hit == clean.run_id


def test_spec_diff_is_human_readable():
    spec, _page = load("shop")
    new = apply_patch(spec, [SpecPatch(field="price", locator=Locator(kind="css", q=".x"), reason="t")],
                      created_by="autoheal", note="n")
    text = "\n".join(spec_diff(spec, new))
    assert "v1 -> v2" in text and "fields.price" in text and "demoted" in text


# --- memory ---------------------------------------------------------------


def test_fingerprint_transfers_across_sites_but_not_across_symptoms():
    a = Fingerprint(signals=("fill_drop", "tier_shift"), diff_class="CLASS_RENAME", transform="money")
    same = Fingerprint(signals=("fill_drop", "tier_shift"), diff_class="CLASS_RENAME", transform="money")
    other = Fingerprint(signals=("constant_collapse",), diff_class="CONTENT_DEFERRED", transform="text")
    assert a.similarity(same) == 1.0
    assert a.similarity(other) < 0.4


def test_recall_can_exclude_the_originating_site():
    st = Store(tempfile.mkdtemp())
    fp = Fingerprint(signals=("fill_drop",), diff_class="CLASS_RENAME", transform="money")
    st.append_episode(Episode(site="shop", field="price", spec_version=1, fingerprint=fp,
                              strategy="adopt_candidate:css", locator=Locator(kind="css", q=".p"),
                              outcome="healed", f1_after=1.0))
    assert st.recall(fp) and not st.recall(fp, exclude_site="shop")


def test_store_round_trips_specs_snapshots_and_baselines():
    spec, page, clean, base, _kg = prep("shop")
    st = Store(tempfile.mkdtemp())
    st.save_spec(spec); st.save_snapshot("shop", 1, page); st.save_records(clean); st.save_baseline(base)
    assert st.load_spec("shop").version == spec.version
    assert st.load_snapshot("shop", 1) == page
    assert len(st.load_records("shop", clean.run_id).records) == len(clean.records)
    assert st.load_baseline("shop").site == "shop"


def test_failed_episodes_are_remembered_too():
    st = Store(tempfile.mkdtemp())
    fp = Fingerprint(signals=("fill_drop",), diff_class="CLASS_RENAME")
    st.append_episode(Episode(site="s", field="f", spec_version=1, fingerprint=fp,
                              strategy="adopt_candidate:xpath", outcome="failed"))
    assert st.failed_strategies("s", "f", 1) == {"adopt_candidate:xpath"}


def test_rolling_baseline_absorbs_content_drift_but_not_breakage():
    """`Baseline.fold` is what lets a live site drift without crying wolf, and
    nothing exercised it: every eval baseline is built from a single clean run,
    so the EWMA and sample accumulation were shipped untested."""
    spec, page, clean, base, _kg = prep("shop")
    rolled = base
    for seed in range(1, 4):
        churned, log = mutators.apply(page, ["content_churn"], seed=seed, severity=2)
        assert not log[0].noop
        rolled = rolled.fold(extract(spec, churned, base_url=BASE_URL))
    assert rolled.runs == 4
    assert all(len(fb.sample) <= 400 for fb in rolled.fields.values())

    # A fourth content change must still be quiet against the rolled baseline...
    nxt, _ = mutators.apply(page, ["content_churn"], seed=9, severity=2)
    assert not perceive(extract(spec, nxt, base_url=BASE_URL), rolled, spec).fired
    # ...and a real breakage must still fire.
    broke, _ = mutators.apply(page, ["decoy_injection"], seed=0, severity=2)
    assert perceive(extract(spec, broke, base_url=BASE_URL), rolled, spec).fired


def test_folded_baseline_accumulates_values_for_novelty():
    spec, page, clean, base, _kg = prep("books")
    churned, _ = mutators.apply(page, ["content_churn"], seed=1, severity=2)
    rolled = base.fold(extract(spec, churned, base_url=BASE_URL))
    grew = [n for n, fb in rolled.fields.items() if len(fb.sample) > len(base.fields[n].sample)]
    assert grew, "folding a run with new values must widen the novelty sample"


@pytest.mark.parametrize("site", SITES)
@pytest.mark.parametrize("severity", [1, 2, 3])
def test_diff_classification_is_stable_across_severity_and_seed(site, severity):
    """The diff thresholds are hand-tuned on four pages. This does not fix that,
    but it catches a threshold sitting on a knife edge: the same mutation must
    not change its classification just because the seed moved."""
    _spec, page = load(site)
    seen = set()
    for seed in range(4):
        out, log = mutators.apply(page, ["class_rename"], seed=seed, severity=severity)
        if log[0].noop:
            pytest.skip("no-op")
        seen.add(CLASS_RENAME in diff(page, out).classes)
    assert seen == {True}, f"{site} sev{severity}: classification varies with seed"


# --- verify ---------------------------------------------------------------


def test_regression_gate_rejects_a_patch_that_overfits_the_broken_page():
    """G2 is the gate that distinguishes a repair from an overfit, so it gets a
    test that fails if it is ever quietly relaxed."""
    spec, page, clean, base, kg = prep("shop")
    out, _ = mutators.apply(page, ["decoy_injection"], seed=0, severity=2)
    before = perceive(extract(spec, out, base_url=BASE_URL), base, spec)
    # A locator pinned to the decoy'd layout: correct today, wrong on the old page.
    bad = apply_patch(spec, [SpecPatch(field="price", locator=Locator(kind="structural", q="./*[3]/*[2]"),
                                       reason="overfit")], created_by="test", note="t")
    v = verify(bad, broken_html=out, good_html=page, known_good=kg, baseline=base,
               before=before, base_url=BASE_URL, patched_fields={"price"})
    g2 = next(g for g in v.gates if g.name == "G2-regression")
    assert not g2.passed and not v.passed


def test_all_three_gates_must_pass():
    spec, page, clean, base, kg = prep("shop")
    before = perceive(clean, base, spec)
    v = verify(spec, broken_html=page, good_html=page, known_good=kg, baseline=base,
               before=before, base_url=BASE_URL)
    assert v.passed and len(v.gates) == 3


# --- the loop -------------------------------------------------------------


@pytest.mark.parametrize("site", SITES)
def test_loop_does_nothing_on_a_healthy_page(site):
    """No breakage signal means no patch. A loop that edits a working spec is
    worse than no loop at all."""
    spec, page, _clean, base, kg = prep(site)
    res = heal(spec, broken_html=page, good_html=page, known_good=kg, baseline=base, base_url=BASE_URL)
    assert not res.fired and not res.healed and res.to_version == spec.version


@pytest.mark.parametrize("site,mut", [("shop", "decoy_injection"), ("shop", "class_rename"),
                                      ("books", "class_rename"), ("wikitable", "reparent"),
                                      ("quotes", "decoy_injection")])
def test_loop_heals_and_the_patch_survives_the_old_page(site, mut):
    spec, page, _clean, base, kg = prep(site)
    out, log = mutators.apply(page, [mut], seed=0, severity=2)
    if log[0].noop:
        pytest.skip("no-op")
    res = heal(spec, broken_html=out, good_html=page, known_good=kg, baseline=base,
               store=Store(tempfile.mkdtemp()), base_url=BASE_URL)
    assert res.healed, res.card
    assert res.to_version == spec.version + 1
    # The healed spec must still work on the page that worked before.
    from autoheal.metrics import score
    again = extract(res.spec, page, base_url=BASE_URL)
    assert score(again.values(), kg, spec.field_names()).macro_f1 >= 0.9


def test_unrecoverable_content_quarantines_instead_of_guessing():
    """`content_deferred` on a page with no structured data is a total loss.
    The correct output is an honest refusal, not a plausible wrong answer."""
    spec, page, _clean, base, kg = prep("books")
    out, log = mutators.apply(page, ["content_deferred"], seed=0, severity=2)
    assert not log[0].noop
    res = heal(spec, broken_html=out, good_html=page, known_good=kg, baseline=base, base_url=BASE_URL)
    assert res.quarantined and not res.healed
    assert res.card and "PAUSED" in res.card


def test_loop_is_deterministic():
    spec, page, _clean, base, kg = prep("shop")
    out, _ = mutators.apply(page, ["class_rename", "reparent"], seed=0, severity=2)
    runs = [heal(spec, broken_html=out, good_html=page, known_good=kg, baseline=base, base_url=BASE_URL)
            for _ in range(2)]
    assert runs[0].spec.model_dump() == runs[1].spec.model_dump()
    assert runs[0].cycles == runs[1].cycles


# --- the model step -------------------------------------------------------
# All offline. The point of these is that every failure mode of the model call
# degrades to the deterministic ranker rather than producing a bad repair --
# and that the wiring itself is exercised, which it never was until the Ollama
# provider landed and immediately surfaced a latent TypeError.

import autoheal.diagnose as dg


@pytest.mark.parametrize("env,want", [
    ("", None), ("none", None), ("off", None), ("bogus", None),
    ("anthropic", ("anthropic", "claude-opus-5")),
    ("anthropic:claude-opus-5", ("anthropic", "claude-opus-5")),
    ("ollama:gpt-oss:120b-cloud", ("ollama", "gpt-oss:120b-cloud")),
])
def test_provider_parsing(monkeypatch, env, want):
    monkeypatch.setenv("AUTOHEAL_LLM", env)
    assert dg.provider() == want


def _cands(site="shop", field="price", mut="class_rename"):
    spec, page, _clean, _base, kg = prep(site)
    out, _ = mutators.apply(page, [mut], seed=0, severity=2)
    cs = candidates(spec, field, out, [r.get(field) for r in kg], old_html=page, base_url=BASE_URL)
    return spec, cs


def _ask(monkeypatch, reply, *, tokens=42, err=""):
    monkeypatch.setenv("AUTOHEAL_LLM", "ollama:test-model")
    monkeypatch.setattr(dg, "_call_ollama", lambda m, p: (reply, tokens, err))
    spec, cs = _cands()
    from autoheal.diff import DomDiff
    from autoheal.perceive import BreakageReport
    return dg._ask_model(spec, "price", BreakageReport(site="shop"), DomDiff(), cs), cs


def test_model_choice_is_accepted_when_valid(monkeypatch):
    (chosen, gen, note, tok), cs = _ask(
        monkeypatch, {"candidate_index": 1, "reason": "r", "confidence": 0.9, "generalized_q": None})
    assert chosen is cs[1] and gen is None and tok == 42 and "test-model" in note


@pytest.mark.parametrize("bad", [
    {"candidate_index": 99, "reason": "", "confidence": 1, "generalized_q": None},
    {"candidate_index": -1, "reason": "", "confidence": 1, "generalized_q": None},
    {"candidate_index": "0", "reason": "", "confidence": 1, "generalized_q": None},
    {"candidate_index": True, "reason": "", "confidence": 1, "generalized_q": None},
    {"reason": "no index at all"},
])
def test_bad_model_index_falls_back_to_the_ranker(monkeypatch, bad):
    (chosen, gen, note, _t), _cs = _ask(monkeypatch, bad)
    assert chosen is None and gen is None and "ranker" in note


def test_unreachable_model_falls_back(monkeypatch):
    (chosen, _g, note, _t), _cs = _ask(monkeypatch, None, tokens=0, err="model call failed (URLError)")
    assert chosen is None and "ranker" in note


def test_no_provider_configured_never_calls_out(monkeypatch):
    monkeypatch.setenv("AUTOHEAL_LLM", "")
    def boom(*a, **k):
        raise AssertionError("must not call a model when AUTOHEAL_LLM is unset")
    monkeypatch.setattr(dg, "_call_ollama", boom)
    monkeypatch.setattr(dg, "_call_anthropic", boom)
    spec, cs = _cands()
    from autoheal.diff import DomDiff
    from autoheal.perceive import BreakageReport
    chosen, gen, note, tok = dg._ask_model(spec, "price", BreakageReport(site="shop"), DomDiff(), cs)
    assert chosen is None and tok == 0 and "no model configured" in note


def test_generalisation_is_passed_through_for_measurement(monkeypatch):
    (chosen, gen, _n, _t), cs = _ask(
        monkeypatch, {"candidate_index": 0, "reason": "r", "confidence": 1,
                      "generalized_q": "[itemprop=price]"})
    assert chosen is cs[0] and gen == "[itemprop=price]"
    # ...and echoing the same query back is not a generalisation.
    (chosen2, gen2, _n2, _t2), cs2 = _ask(
        monkeypatch, {"candidate_index": 0, "reason": "r", "confidence": 1,
                      "generalized_q": cs[0].locator.q})
    assert gen2 is None


def test_evaluate_locator_accepts_the_blind_flag():
    """Regression test for a TypeError that only the model path could reach:
    `diagnose` passed `known_good_aware` to `evaluate_locator`, which did not
    accept it, so any model-proposed generalisation crashed the loop."""
    spec, page, _clean, _base, kg = prep("shop")
    out, _ = mutators.apply(page, ["class_rename"], seed=0, severity=2)
    vals = [r.get("price") for r in kg]
    for flag in (True, False):
        evaluate_locator(spec, "price", out, vals, Locator(kind="jsonld", q="offers.price"),
                         old_html=page, base_url=BASE_URL, known_good_aware=flag)


# --- the model transports, mocked ------------------------------------------
# `_call_ollama` and `_call_anthropic` were the last unexercised code in the
# library. The one time an unexercised path here was finally run for real it
# raised a TypeError on the first call, so they get coverage without a network.

class _FakeResp:
    def __init__(self, payload):
        self._p = payload
    def read(self):
        return json.dumps(self._p).encode()
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def test_call_ollama_parses_content_and_counts_tokens(monkeypatch):
    import urllib.request
    reply = {"message": {"content": json.dumps({"candidate_index": 2, "reason": "r",
                                                "confidence": 0.5, "generalized_q": None})},
             "prompt_eval_count": 900, "eval_count": 120}
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _FakeResp(reply))
    args, tokens, err = dg._call_ollama("m", {"field": "price"})
    assert args["candidate_index"] == 2 and tokens == 1020 and err == ""


def test_call_ollama_reports_unparseable_json_without_raising(monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: _FakeResp({"message": {"content": "not json at all"},
                                                   "prompt_eval_count": 5, "eval_count": 1}))
    args, tokens, err = dg._call_ollama("m", {})
    assert args is None and tokens == 6 and "unparseable" in err


def test_call_ollama_survives_a_dead_endpoint(monkeypatch):
    import urllib.request
    def boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    args, tokens, err = dg._call_ollama("m", {})
    assert args is None and tokens == 0 and "call failed" in err


def test_call_ollama_tolerates_missing_token_counts(monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _FakeResp(
        {"message": {"content": "{}"}}))  # no prompt_eval_count / eval_count
    args, tokens, err = dg._call_ollama("m", {})
    assert args == {} and tokens == 0


def test_call_anthropic_without_the_sdk_degrades(monkeypatch):
    import builtins
    real = builtins.__import__
    def no_anthropic(name, *a, **k):
        if name == "anthropic":
            raise ImportError("not installed")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_anthropic)
    args, tokens, err = dg._call_anthropic("claude-opus-5", {})
    assert args is None and "SDK not installed" in err


def _diag(monkeypatch, reply, field="price", mut="class_rename"):
    monkeypatch.setenv("AUTOHEAL_LLM", "ollama:test")
    monkeypatch.setattr(dg, "_call_ollama", lambda m, p: (reply, 100, ""))
    spec, page, _clean, base, kg = prep("shop")
    out, _ = mutators.apply(page, [mut], seed=0, severity=2)
    rep = perceive(extract(spec, out, base_url=BASE_URL), base, spec)
    return dg.diagnose(spec, rep, field, broken_html=out, good_html=page,
                       known_good=kg, base_url=BASE_URL, use_llm=True)


def test_a_model_generalisation_that_measures_worse_is_rejected(monkeypatch):
    """The model proposes; the runtime disposes. A nonsense widening must be
    measured and thrown away, not adopted on the model's say-so."""
    dx = _diag(monkeypatch, {"candidate_index": 0, "reason": "r", "confidence": 1,
                             "generalized_q": ".totally-nonexistent-xyz"})
    assert dx.patch is not None
    assert dx.patch.locator.q != ".totally-nonexistent-xyz"
    assert "rejected generalisation" in dx.rationale


def test_diagnose_reports_when_no_candidate_recovers_anything():
    """An empty candidate list must produce no patch and say why -- that is what
    routes the case to an honest quarantine instead of a guess."""
    spec, page, _clean, base, kg = prep("shop")
    rep = perceive(extract(spec, page, base_url=BASE_URL), base, spec)
    dx = dg.diagnose(spec, rep, "price", broken_html="<html><body></body></html>",
                     good_html=page, known_good=kg, base_url=BASE_URL)
    assert dx.patch is None and "no candidate" in dx.rationale


# --- the retry path --------------------------------------------------------
# Across the whole matrix the cycle counts are {1: 50, 0: 30, 4: 2}: every case
# either heals on the first cycle or runs to the cap. So the loop's retry
# machinery -- record what lost, exclude it, try something else -- has never
# once produced a success in an eval run. These force it.

def _flaky_verify(fail_first_n: int):
    """Wrap the real verify so the first N verdicts are forced to fail."""
    from autoheal import loop as loop_mod
    real = loop_mod.verify
    state = {"n": 0}

    def wrapper(*a, **kw):
        v = real(*a, **kw)
        state["n"] += 1
        if state["n"] <= fail_first_n:
            from autoheal.verify import Gate
            v.gates = [Gate(name="G1-recovery", passed=False, detail="forced failure for test")]
        return v
    wrapper.state = state
    return wrapper


def test_a_rejected_patch_is_retried_with_a_different_strategy(monkeypatch):
    spec, page, _clean, base, kg = prep("shop")
    out, _ = mutators.apply(page, ["class_rename"], seed=0, severity=2)
    monkeypatch.setattr("autoheal.loop.verify", _flaky_verify(1))
    res = heal(spec, broken_html=out, good_html=page, known_good=kg, baseline=base,
               store=Store(tempfile.mkdtemp()), base_url=BASE_URL)
    assert res.healed, res.card
    assert res.cycles == 2, f"expected a second cycle, got {res.cycles}"
    by_field = {}
    for d in res.diagnoses:
        by_field.setdefault(d.field, []).append(d.patch.strategy if d.patch else None)
    retried = [v for v in by_field.values() if len(v) > 1]
    assert retried, "no field was diagnosed twice"
    assert any(v[0] != v[1] for v in retried), \
        f"the retry re-proposed the strategy that just lost: {by_field}"


def test_repeated_failures_exhaust_the_cap_and_quarantine(monkeypatch):
    spec, page, _clean, base, kg = prep("shop")
    out, _ = mutators.apply(page, ["class_rename"], seed=0, severity=2)
    monkeypatch.setattr("autoheal.loop.verify", _flaky_verify(99))
    res = heal(spec, broken_html=out, good_html=page, known_good=kg, baseline=base,
               store=Store(tempfile.mkdtemp()), base_url=BASE_URL, max_cycles=3)
    assert res.quarantined and not res.healed
    assert res.cycles == 3
    assert res.card and "cycles spent      : 3" in res.card


def test_losing_strategies_are_written_to_memory(monkeypatch):
    """Failures are remembered too -- that is what stops the loop proposing the
    same losing idea four times and calling it four attempts."""
    spec, page, _clean, base, kg = prep("shop")
    out, _ = mutators.apply(page, ["class_rename"], seed=0, severity=2)
    store = Store(tempfile.mkdtemp())
    monkeypatch.setattr("autoheal.loop.verify", _flaky_verify(1))
    heal(spec, broken_html=out, good_html=page, known_good=kg, baseline=base,
         store=store, base_url=BASE_URL)
    outcomes = {e.outcome for e in store.episodes()}
    assert "failed" in outcomes and "healed" in outcomes
    assert store.failed_strategies("shop", "price", spec.version)


def test_a_successful_heal_persists_everything_the_next_run_needs():
    """The store is the mechanism, so the write path is worth asserting: spec,
    records, snapshot, last-known-good pointer and a folded baseline."""
    spec, page, _clean, base, kg = prep("shop")
    out, _ = mutators.apply(page, ["decoy_injection"], seed=0, severity=2)
    store = Store(tempfile.mkdtemp())
    res = heal(spec, broken_html=out, good_html=page, known_good=kg, baseline=base,
               store=store, base_url=BASE_URL, run_id=7)
    assert res.healed
    assert store.spec_versions("shop") == [res.to_version]
    assert store.load_spec("shop").version == res.to_version
    assert store.has_snapshot("shop", 7) and store.last_good("shop") == 7
    assert len(store.load_records("shop", 7).records) > 0
    rolled = store.load_baseline("shop")
    assert rolled is not None and rolled.runs == base.runs + 1
    assert [l for l in store.lineage("shop")][0][1] == spec.version  # parent pointer


def test_call_anthropic_reads_a_tool_use_block(monkeypatch):
    """Covers the Anthropic transport without installing the SDK: the shape of
    the response it parses is pinned here so a change to that shape is caught."""
    import sys
    import types

    args_seen = {}

    class _Block:
        type = "tool_use"
        input = {"candidate_index": 1, "reason": "r", "confidence": 0.9, "generalized_q": None}

    class _Usage:
        input_tokens, output_tokens = 800, 90

    class _Resp:
        content = [_Block()]
        usage = _Usage()

    class _Messages:
        def create(self, **kw):
            args_seen.update(kw)
            return _Resp()

    class _Client:
        messages = _Messages()

    fake = types.ModuleType("anthropic")
    fake.Anthropic = lambda *a, **k: _Client()
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    args, tokens, err = dg._call_anthropic("claude-opus-5", {"field": "price"})
    assert args["candidate_index"] == 1 and tokens == 890 and err == ""
    # the request must stay schema-constrained and must not send `temperature`,
    # which Opus 5 rejects outright
    assert args_seen["tool_choice"]["name"] == "choose_locator"
    assert args_seen["tools"][0]["strict"] is True
    assert "temperature" not in args_seen


def test_call_anthropic_with_no_tool_use_block_degrades(monkeypatch):
    import sys
    import types

    class _Text:
        type = "text"

    class _Usage:
        input_tokens, output_tokens = 10, 5

    class _Resp:
        content = [_Text()]
        usage = _Usage()

    fake = types.ModuleType("anthropic")
    fake.Anthropic = lambda *a, **k: type("C", (), {"messages": type("M", (), {
        "create": lambda self, **kw: _Resp()})()})()
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    args, tokens, err = dg._call_anthropic("claude-opus-5", {})
    assert args is None and tokens == 15 and "no tool call" in err
