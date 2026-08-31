"""Record the demo by running the real pipeline and serialising what it returns.

Hard rule: no event in `events.json` is authored by hand. Every number, evidence
string, candidate list, gate result and spec diff on the dashboard comes back
from `perceive` / `diff` / `localize` / `verify` on a real run. A demo that
narrates numbers the system did not produce is the exact failure this project
exists to catch, and it would be embarrassing to ship it inside this repo.

Three acts, chosen because they are the ones the measurements support:

  I + II  quotes, decoy injected -- fill stays 100%, F1 collapses to 0.33, and
          nothing errors. Then the loop repairs it and the gates go green.
  III     shop, the SAME mutation class on a DIFFERENT site. The episode quotes
          left behind resolves one of shop's two ambiguous decisions for free,
          which is a measured token saving, not a claim.

Run:  AUTOHEAL_LLM=ollama:gpt-oss:120b-cloud uv run python -m demo.record
Without AUTOHEAL_LLM it still records; the token counters are simply zero
because the deterministic ranker settled every decision on its own.
"""

from __future__ import annotations

import json
import pathlib
import tempfile

from autoheal.diff import diff
from autoheal.loop import heal
from autoheal.memory import Store
from autoheal.metrics import score
from autoheal.perceive import RECORD, Baseline, perceive
from autoheal.runtime import extract
from eval import mutators
from eval.harness import BASE_URL, load, truth

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "events.json"
TEMPLATE = HERE / "dashboard.template.html"
REPLAY = HERE / "replay.html"
MUTATION = ["decoy_injection"]
SEED, SEVERITY = 0, 2


class Recorder:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def add(self, kind: str, *, act: int, hold: int = 600, **payload) -> None:
        self.events.append({"kind": kind, "act": act, "hold": hold, **payload})


def prepare(site: str):
    spec, page = load(site)
    clean = extract(spec, page, base_url=BASE_URL)
    return spec, page, clean, Baseline.observe(clean), clean.values()


def record_break(ev: Recorder, act: int, site: str, spec, page, baseline, kg):
    """Acts I and II share a breakage; this emits everything up to the repair."""
    broken, log = mutators.apply(page, MUTATION, seed=SEED, severity=SEVERITY)
    marker = log[0].detail.split("marked .")[1].split()[0]
    run = extract(spec, broken, base_url=BASE_URL)
    s = score(run.values(), truth(site), spec.field_names())

    ev.add("page", act=act, site=site, clean_html=page, broken_html=broken,
           mutation=log[0].detail, marker=marker, hold=1200)

    # --- Act I: the static extractor, still confidently writing wrong records.
    wrong = [f for f, v in s.fields.items() if v[2] < 0.99]
    ev.add("static", act=act, site=site,
           records=len(run.records), fill=round(sum(s.fill.values()) / len(s.fill), 3),
           f1=round(s.macro_f1, 3), silent=bool(s.silent), wrong_fields=sorted(wrong),
           sample=[{"field": f, "got": run.values()[0].get(f), "expected": truth(site)[0].get(f)}
                   for f in sorted(wrong)],
           hold=2500)

    # --- Act II: the monitor, with its own words.
    rep = perceive(run, baseline, spec)
    ev.add("health", act=act, site=site, fired=rep.fired, broken_fields=rep.broken,
           scores={f: round(h.score, 3) for f, h in rep.fields.items() if h.severity != "ok"},
           hold=900)
    for f in rep.broken:
        for sig in rep.fields[f].signals:
            ev.add("signal", act=act, field=f, name=sig.name,
                   magnitude=round(sig.magnitude, 3), evidence=sig.evidence, hold=700)
    ev.add("diff", act=act, summary=diff(page, broken).summary(),
           evidence=diff(page, broken).evidence(), hold=900)
    return broken, rep


def record_repair(ev: Recorder, act: int, site: str, spec, page, broken, baseline, kg,
                  store, *, use_llm: bool, label: str = ""):
    res = heal(spec, broken_html=broken, good_html=page, known_good=kg, baseline=baseline,
               store=store, base_url=BASE_URL, use_llm=use_llm)
    for d in res.diagnoses:
        ev.add("candidates", act=act, field=d.field, label=label,
               items=[{"kind": c.locator.kind, "q": c.locator.q, "attr": c.locator.attr,
                       "recovery": c.recovery, "coverage": c.coverage, "prior": c.prior,
                       "survives_old": c.survives_old, "score": c.score}
                      for c in d.candidates],
               recalled=[r.as_prior() for r in d.recalls],
               used_memory=d.used_memory, used_llm=d.used_llm, tokens=d.tokens,
               cost=d.cost_note(), hold=1400)
        if d.patch:
            ev.add("patch", act=act, field=d.field, label=label,
                   describe=d.patch.describe(), strategy=d.patch.strategy, hold=1100)
    for g in res.gates:
        name, _, state = g.partition("=")
        ev.add("gate", act=act, name=name, passed=(state == "pass"), label=label, hold=800)
    ev.add("specdiff", act=act, lines=res.spec_diff, label=label, hold=2000)
    ev.add("result", act=act, site=site, label=label, healed=res.healed,
           f1_before=res.f1_before, f1_after=res.f1_after, cycles=res.cycles,
           tokens=res.tokens, llm_calls=res.llm_calls, avoided=res.llm_calls_avoided,
           version=f"v{res.from_version}->v{res.to_version}", card=res.card, hold=2200)
    return res


def main() -> int:
    ev = Recorder()
    store = Store(tempfile.mkdtemp(prefix="autoheal-demo-"))

    # ---- Acts I & II -----------------------------------------------------
    spec, page, clean, baseline, kg = prepare("quotes")
    ev.add("act", act=1, title="A scraper that fails silently",
           subtitle="quotes.toscrape.com, frozen. A decoy is injected ahead of every quote.", hold=1500)
    broken, _rep = record_break(ev, 1, "quotes", spec, page, baseline, kg)
    ev.add("act", act=2, title="The heal",
           subtitle="perceive -> diagnose -> patch -> verify -> remember", hold=1200)
    first = record_repair(ev, 2, "quotes", spec, page, broken, baseline, kg, store, use_llm=True)

    # ---- Act III ---------------------------------------------------------
    spec2, page2, clean2, baseline2, kg2 = prepare("shop")
    ev.add("act", act=3, title="The second one was cheaper",
           subtitle="Same mutation class, different site. The only episodes in memory are quotes'.",
           hold=1500)
    broken2, log2 = mutators.apply(page2, MUTATION, seed=SEED, severity=SEVERITY)
    ev.add("page", act=3, site="shop", clean_html=page2, broken_html=broken2,
           mutation=log2[0].detail, marker=log2[0].detail.split("marked .")[1].split()[0],
           hold=1200)
    # Act III gets its own static readout. Without it the panel kept showing Act
    # I's quotes numbers under a header that said "shop" -- stale data presented
    # as current, which is the failure this project is about, on our own slide.
    run2 = extract(spec2, broken2, base_url=BASE_URL)
    s2 = score(run2.values(), truth("shop"), spec2.field_names())
    wrong2 = [f for f, v in s2.fields.items() if v[2] < 0.99]
    ev.add("static", act=3, site="shop", records=len(run2.records),
           fill=round(sum(s2.fill.values()) / len(s2.fill), 3), f1=round(s2.macro_f1, 3),
           silent=bool(s2.silent), wrong_fields=sorted(wrong2),
           sample=[{"field": f, "got": run2.values()[0].get(f),
                    "expected": truth("shop")[0].get(f)} for f in sorted(wrong2)],
           hold=2200)
    ev.add("note", act=3, text=f"memory holds {len(store.episodes())} episodes, all from "
           f"{sorted({e.site for e in store.episodes()})} -- nothing from shop", hold=1500)
    with_mem = record_repair(ev, 3, "shop", spec2, page2, broken2, baseline2, kg2, store,
                             use_llm=True, label="with memory")
    without = heal(spec2, broken_html=broken2, good_html=page2, known_good=kg2,
                   baseline=baseline2, store=None, base_url=BASE_URL, use_llm=True)
    ev.add("ab", act=3,
           with_memory={"calls": with_mem.llm_calls, "tokens": with_mem.tokens,
                        "avoided": with_mem.llm_calls_avoided, "healed": with_mem.healed,
                        "f1": with_mem.f1_after},
           ablated={"calls": without.llm_calls, "tokens": without.tokens,
                    "avoided": without.llm_calls_avoided, "healed": without.healed,
                    "f1": without.f1_after},
           hold=3000)

    blob = json.dumps({"events": ev.events}, indent=1)
    OUT.write_text(blob)
    # Inline the log into the page. A file:// page cannot fetch(), and a demo
    # that needs a server is a demo that can fail on stage.
    #
    # The snapshots we embed contain `<script type="application/ld+json">` tags,
    # and the FIRST `</script>` inside the JSON closes the host <script> block
    # early -- the rest of the page HTML then leaks into the document as markup.
    # `<\/` is a valid JSON escape for `</`, so JSON.parse restores it exactly.
    safe = blob.replace("</", "<\\/")
    REPLAY.write_text(TEMPLATE.read_text().replace("/*__EVENTS__*/", safe))
    print(f"recorded {len(ev.events)} events -> {OUT} ({OUT.stat().st_size/1024:.0f} KB)")
    print(f"self-contained player -> {REPLAY} ({REPLAY.stat().st_size/1024:.0f} KB)")
    print(f"  act I/II  quotes: F1 {first.f1_before:.2f} -> {first.f1_after:.2f}, "
          f"{first.llm_calls} model call(s), {first.tokens} tokens")
    print(f"  act III   shop  : with memory {with_mem.llm_calls} call(s)/{with_mem.tokens} tok"
          f"   ablated {without.llm_calls} call(s)/{without.tokens} tok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
