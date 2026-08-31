"""The recorded demo must be self-contained and must contain only real data.

`demo/replay.html` is the artefact an audience actually sees, and it had a bug
that no unit test would have caught and that looked fine from the outside: the
snapshots it embeds contain `<script type="application/ld+json">`, and the first
`</script>` inside the JSON closed the host <script> block early, spilling raw
page HTML into the document and leaving the player dead. The file was written,
the byte count looked right, and the page was broken.
"""

import json

import pytest

import demo.record as rec


@pytest.fixture(scope="module")
def recorded(tmp_path_factory):
    """Record into a temp dir so the committed demo is never clobbered."""
    d = tmp_path_factory.mktemp("demo")
    out, replay = d / "events.json", d / "replay.html"
    old = (rec.OUT, rec.REPLAY)
    rec.OUT, rec.REPLAY = out, replay
    try:
        assert rec.main() == 0
        return json.loads(out.read_text())["events"], replay.read_text()
    finally:
        rec.OUT, rec.REPLAY = old


def test_replay_is_self_contained(recorded):
    _events, html = recorded
    assert "fetch(" not in html, "a file:// page cannot fetch; data must be inlined"
    assert "/*__EVENTS__*/" not in html, "template placeholder was not substituted"
    assert "http://" not in html.split("<script")[0]


def test_embedded_json_does_not_break_out_of_its_script_tag(recorded):
    """The regression. Snapshots contain </script>; it must be escaped so the
    host block survives and the parsed data still round-trips exactly."""
    events, html = recorded
    blob = html.split('type="application/json">', 1)[1].split("</script>", 1)[0]
    assert "</script>" not in blob, "raw </script> in the payload closes the block early"
    assert json.loads(blob)["events"] == events, "escaping must survive JSON.parse"


def test_every_act_is_present_and_ordered(recorded):
    events, _html = recorded
    acts = [e["act"] for e in events if e["kind"] == "act"]
    assert acts == [1, 2, 3]
    assert [e["act"] for e in events] == sorted(e["act"] for e in events)


def test_the_demo_shows_a_real_silent_failure_then_a_real_repair(recorded):
    """Guard against the demo drifting into narrating numbers the system did not
    produce -- the exact failure this project is about."""
    events, _html = recorded
    static = next(e for e in events if e["kind"] == "static")
    assert static["silent"] and static["fill"] >= 0.9 and static["f1"] < 0.9

    results = [e for e in events if e["kind"] == "result"]
    assert results and all(r["healed"] for r in results)
    first = results[0]
    assert first["f1_after"] > first["f1_before"] and first["f1_after"] >= 0.9

    gates = [e for e in events if e["kind"] == "gate"]
    assert gates and all(g["passed"] for g in gates)
    assert {g["name"] for g in gates} == {"G1-recovery", "G2-regression", "G3-clearance"}


def test_act_three_is_a_cross_site_recall_not_a_repeat(recorded):
    """Act III's claim is that memory transfers. The A/B must show a real saving
    and both arms must still heal -- memory makes it cheaper, not better."""
    events, _html = recorded
    ab = next(e for e in events if e["kind"] == "ab")
    assert ab["with_memory"]["healed"] and ab["ablated"]["healed"]
    assert ab["with_memory"]["calls"] < ab["ablated"]["calls"]
    assert ab["with_memory"]["f1"] == ab["ablated"]["f1"]
    note = next(e for e in events if e["kind"] == "note")
    assert "shop" not in note["text"].split("--")[0], "memory must hold no shop episodes yet"


def test_candidate_tables_carry_measured_evidence(recorded):
    events, _html = recorded
    cands = [e for e in events if e["kind"] == "candidates"]
    assert cands
    for c in cands:
        assert c["items"], f"{c['field']} has no candidates"
        for it in c["items"]:
            assert 0.0 <= it["recovery"] <= 1.0 and 0.0 <= it["prior"] <= 1.0
