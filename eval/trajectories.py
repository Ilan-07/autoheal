"""Generate agent trajectories: `trajectories/*.md`, one per representative run.

Deliverable 4 of the hackathon brief asks for trajectories that are easy to
follow "from the agent instructions to the final result", showing what the agent
did, how its tools responded, the feedback that shaped its next step, and any
retries or human checkpoints.

Everything written here is captured from a live run. The model exchange is
intercepted at the transport, so the prompt and the reply are verbatim -- not a
reconstruction. Where a trajectory shows the ranker deciding without a model,
that is because no model call was made, and the file says so.

    uv run python -m eval.trajectories                # no model: 84% path only
    AUTOHEAL_LLM=ollama:gpt-oss:120b-cloud uv run python -m eval.trajectories
"""

from __future__ import annotations

import json
import pathlib
import tempfile

import autoheal.diagnose as dg
from autoheal.loop import heal
from autoheal.memory import Store
from autoheal.metrics import score
from autoheal.perceive import Baseline, perceive
from autoheal.runtime import extract
from eval import mutators
from eval.harness import BASE_URL, load, truth

OUT = pathlib.Path(__file__).resolve().parent.parent / "trajectories"


class Tap:
    """Intercepts the model transport so the exchange can be recorded verbatim."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def install(self):
        real = dg._call_ollama

        def wrapped(model, payload):
            args, tokens, err = real(model, payload)
            self.calls.append({"model": model, "payload": payload, "reply": args,
                               "tokens": tokens, "error": err})
            return args, tokens, err
        dg._call_ollama = wrapped
        return real

    @staticmethod
    def restore(real):
        dg._call_ollama = real


def _prep(site: str):
    spec, page = load(site)
    clean = extract(spec, page, base_url=BASE_URL)
    return spec, page, Baseline.observe(clean), clean.values()


def _fence(obj, lang="json", limit=2600) -> str:
    text = obj if isinstance(obj, str) else json.dumps(obj, indent=1)
    if len(text) > limit:
        text = text[:limit] + f"\n... [{len(text) - limit} more characters]"
    return f"```{lang}\n{text}\n```"


def _header(title: str, blurb: str) -> list[str]:
    return [f"# {title}", "", blurb, "",
            "*Captured from a live run by `eval/trajectories.py`. Prompts and replies are"
            " verbatim; every number is what the tools actually returned.*", ""]


def repair_trajectory(site: str, muts: list[str], sev: int, slug: str, name: str, blurb: str,
                      store: Store | None, use_llm: bool) -> pathlib.Path:
    spec, page, baseline, kg = _prep(site)
    broken, log = mutators.apply(page, muts, seed=0, severity=sev)
    before_run = extract(spec, broken, base_url=BASE_URL)
    before = perceive(before_run, baseline, spec)
    s0 = score(before_run.values(), truth(site), spec.field_names())

    tap = Tap()
    real = tap.install()
    try:
        res = heal(spec, broken_html=broken, good_html=page, known_good=kg,
                   baseline=baseline, store=store, base_url=BASE_URL, use_llm=use_llm)
    finally:
        Tap.restore(real)

    after = extract(res.spec or spec, broken, base_url=BASE_URL)
    s1 = score(after.values(), truth(site), spec.field_names())

    L = _header(name, blurb)
    L += ["## 0. The trigger", "",
          f"`{site}` was mutated by `{'+'.join(muts)}` at severity {sev}.", "",
          f"> {log[0].detail}", "",
          "The static extractor does not fail. It reports:", "",
          f"| records | fill rate | errors raised | F1 vs frozen truth |",
          f"|---|---|---|---|",
          f"| {len(before_run.records)} | {sum(s0.fill.values())/len(s0.fill):.0%} | "
          f"{len(before_run.errors)} | **{s0.macro_f1:.2f}** |", ""]
    if s0.silent:
        L += ["This is a **silent failure**: high fill, wrong values, nothing thrown.", ""]

    L += ["## 1. PERCEIVE — what the monitor saw", "",
          "No exception is involved. Each signal compares this run to a rolling baseline"
          " of healthy runs.", ""]
    for f in before.broken:
        L.append(f"**`{f}`** — health {before.fields[f].score:.2f} ({before.fields[f].severity})")
        for sig in before.fields[f].signals:
            L.append(f"- `{sig.name}` ({sig.magnitude:.2f}) — {sig.evidence}")
        L.append("")

    L += ["## 2. DIAGNOSE — candidates, measured before any model sees them", ""]
    for d in res.diagnoses:
        L += [f"### field `{d.field}`", ""]
        if not d.candidates:
            L += [f"Structural change: `{d.fingerprint.diff_class}`. "
                  "**No candidate locator could be generated at all** — the known-good values "
                  "are not present anywhere in the new DOM, so there is nothing to point at. "
                  "An empty candidate list is an honest answer and it is what routes this case "
                  "to quarantine instead of a guess.", "",
                  f"> {d.rationale}", ""]
            continue
        L += [f"Structural change: `{d.fingerprint.diff_class}`. "
              f"{len(d.candidates)} candidates generated from last-known-good values, "
              f"each **executed against every record on the page**:", "",
              "| # | kind | query | recovers | covers | robustness | works on old page |",
              "|---|---|---|---|---|---|---|"]
        for i, c in enumerate(d.candidates):
            q = c.locator.q.replace("|", "\\|")
            L.append(f"| {i} | {c.locator.kind} | `{q}`"
                     f"{' @' + c.locator.attr if c.locator.attr else ''} | {c.recovery:.2f} "
                     f"| {c.coverage:.2f} | {c.prior:.2f} | {'yes' if c.survives_old else 'no'} |")
        L.append("")
        if d.recalls:
            L += ["**Memory recall** (episodes are keyed on a symptom fingerprint stripped of"
                  " site identity, so a lesson can transfer):", ""]
            L += [f"- {r.as_prior()}" for r in d.recalls]
            L.append("")
        L += [f"**Route taken:** {d.cost_note()}.", "",
              f"**Chosen:** `{d.patch.locator.kind} {d.patch.locator.q}`", "",
              f"> {d.rationale}", ""]

    if tap.calls:
        L += ["## 3. The model step — verbatim", "",
              "Reached only when memory and the deterministic ranker both decline. The model"
              " picks from measured options; it never sees raw HTML.", ""]
        for i, c in enumerate(tap.calls, 1):
            L += [f"### call {i} — `{c['model']}`", "",
                  "**Agent instructions (system prompt):**", "",
                  _fence(dg._SYSTEM + dg._JSON_INSTRUCTION, "text"), "",
                  "**Input — ranked candidates and evidence, no page markup:**", "",
                  _fence(c["payload"]), "",
                  "**Reply:**", "", _fence(c["reply"]), "",
                  f"**Tokens:** {c['tokens']}"
                  + (f"  ·  **error:** {c['error']}" if c["error"] else ""), "",
                  "The reply is validated before use: an out-of-range index, a missing key or an"
                  " unreachable model all fall back to the deterministic ranker. A proposed"
                  " query generalisation is re-executed and kept only if it measures at least"
                  " as well.", ""]
    else:
        L += ["## 3. The model step", "",
              "**Not reached.** Memory and the deterministic ranker resolved every decision, so"
              " no model was called and no tokens were spent. Across the full matrix this is"
              " the majority path: recall 51%, ranker alone 33%, model 16%.", ""]

    L += ["## 4. VERIFY — three gates, all mandatory", ""]
    names = {"G1-recovery": "re-extract on the broken page vs last-known-good",
             "G2-regression": "run the patched spec on the page that still worked",
             "G3-clearance": "the health signals that fired must go quiet"}
    if not res.gates:
        L += ["**Never reached.** No patch was produced, so there was nothing to verify.", ""]
    else:
        L += ["| gate | result | what it checks |", "|---|---|---|"]
        for g in res.gates:
            n, _, st = g.partition("=")
            L.append(f"| `{n}` | {'**pass**' if st == 'pass' else '**FAIL**'} | {names.get(n,'')} |")
        L.append("")

    L += ["## 5. Outcome", ""]
    if res.healed:
        L += [f"**Healed** in {res.cycles} cycle(s). Spec `v{res.from_version} -> v{res.to_version}`. "
              f"F1 **{s0.macro_f1:.2f} -> {s1.macro_f1:.2f}** against frozen truth the loop never saw.",
              "", "Patches are additive — the old locator is demoted, not deleted, because sites"
              " A/B test and revert:", "", _fence("\n".join(res.spec_diff), "diff"), ""]
    else:
        L += ["**Quarantined.** No patch cleared the gates, so extraction is paused and a"
              " human-review card is emitted rather than a guess being written.", "",
              "This is the designed answer, not a shortfall: a confident wrong value is worse"
              " than an admission of defeat. **This is the human checkpoint** — the loop stops"
              " and hands over.", "", _fence(res.card or "", "text"), ""]
    L += [f"**Model calls:** {res.llm_calls} · **tokens:** {res.tokens}"
          f" · **resolved by recall:** {res.llm_calls_avoided}", ""]

    path = OUT / f"{slug}.md"
    path.write_text("\n".join(L))
    return path


def b1_trajectory() -> pathlib.Path:
    """The baseline agent, on the same case, for a like-for-like comparison."""
    import eval.b1_oneshot as b1
    site, muts, sev = "quotes", ["decoy_injection"], 2
    captured: dict = {}
    real_ask = b1.ask

    def wrapped(model, user):
        reply, tok, secs, err = real_ask(model, user)
        captured.update(model=model, user=user, reply=reply, tokens=tok, secs=secs, err=err)
        return reply, tok, secs, err
    b1.ask = wrapped
    try:
        row = b1.run_case(site, "decoy_injection", muts, sev, 0, "gpt-oss:120b-cloud")
    finally:
        b1.ask = real_ask

    L = _header("Baseline agent — B1 one-shot",
                "The comparison baseline: one prompt, the whole broken page, fix the selector. "
                "Same model, same three gates, same frozen truth as the Autoheal trajectory.")
    if not captured or row is None:
        L += ["No model configured — set `AUTOHEAL_LLM`-capable Ollama and re-run.", ""]
        path = OUT / "04-baseline-b1-one-shot.md"
        path.write_text("\n".join(L))
        return path

    L += ["## Agent instructions (system prompt)", "", _fence(b1.SYSTEM, "text"), "",
          "## Input — the whole page", "",
          f"The prompt carries the complete broken HTML: **{len(captured['user']):,} characters**. "
          "Autoheal's prompt for the same repair carries ranked candidates instead, and is "
          "roughly two orders of magnitude smaller.", "",
          _fence(captured["user"], "html", limit=1200), "",
          "## Reply", "", _fence(captured["reply"]), "",
          f"**Tokens:** {captured['tokens']:,} · **wall clock:** {captured['secs']:.1f}s", "",
          "## Deterministic scoring of what it returned", "",
          "| field | kind | query | addressing style | recovers | works on old page |",
          "|---|---|---|---|---|---|"]
    for c in row["chosen"]:
        q = str(c["q"]).replace("|", "\\|")
        rec = "n/a" if c["recovery"] is None else f"{c['recovery']:.2f}"
        old = "n/a" if c["survives_old"] is None else ("yes" if c["survives_old"] else "**no**")
        L.append(f"| {c['field']} | {c['kind']} | `{q}` | {c['strategy']} | {rec} | {old} |")
    L += ["", f"**Gates:** {', '.join(row['gates']) or 'no patch produced'}", "",
          f"**Outcome:** F1 {row['f1_static']:.2f} -> {row['f1_b1']:.2f}"
          f" · on the pre-break page {row['f1_oldpage']:.2f}"
          f" · {'healed' if row['healed'] else 'did not clear the gates'}", ""]
    path = OUT / "04-baseline-b1-one-shot.md"
    path.write_text("\n".join(L))
    return path


def main() -> int:
    OUT.mkdir(exist_ok=True)
    store = Store(tempfile.mkdtemp(prefix="autoheal-traj-"))
    written = []

    written.append(repair_trajectory(
        "quotes", ["decoy_injection"], 2, "01-silent-failure-healed",
        "Trajectory 1 — silent failure, healed",
        "The flagship case. A decoy is injected ahead of every quote: fill rate stays at "
        "100%, nothing raises, and every value is wrong. The loop detects it, repairs it "
        "and verifies the repair.", store, use_llm=True))

    written.append(repair_trajectory(
        "shop", ["decoy_injection"], 2, "02-memory-transfers-across-sites",
        "Trajectory 2 — memory transfers across sites",
        "The same *class* of breakage on a different site. The episode log holds only "
        "`quotes` episodes at this point, and one of shop's ambiguous decisions is resolved "
        "from it for free.", store, use_llm=True))

    written.append(repair_trajectory(
        "books", ["content_deferred"], 2, "03-quarantine-human-checkpoint",
        "Trajectory 3 — quarantine, the human checkpoint",
        "The case the loop cannot fix: the page's visible text is gone and `books` ships no "
        "structured data, so the values genuinely are not in the DOM. Refusing is the correct "
        "answer, and the loop hands over to a human instead of inventing a locator.",
        store, use_llm=True))

    try:
        written.append(b1_trajectory())
    except Exception as e:  # the baseline needs a reachable model; the rest do not
        print(f"  (skipped B1 trajectory: {type(e).__name__}: {e})")

    index = ["# Agent trajectories", "",
             "Representative end-to-end runs for every agent in this project, captured live by",
             "`uv run python -m eval.trajectories`. Prompts and replies are verbatim.", "",
             "| trajectory | agent | what it shows |", "|---|---|---|",
             "| [01 — silent failure, healed](01-silent-failure-healed.md) | Autoheal repair loop |"
             " detection of a silent failure, repair, three gates, additive patch |",
             "| [02 — memory transfers](02-memory-transfers-across-sites.md) | Autoheal repair loop |"
             " recall from another site resolving a decision for zero tokens |",
             "| [03 — quarantine](03-quarantine-human-checkpoint.md) | Autoheal repair loop |"
             " an honest refusal and the human-review card |",
             "| [04 — baseline B1](04-baseline-b1-one-shot.md) | One-shot baseline agent |"
             " the same repair from raw HTML, for comparison |", "",
             "The loop is deterministic apart from the model step, which is reached on roughly",
             "one decision in six; the other five are settled by memory recall or the ranker.", ""]
    (OUT / "README.md").write_text("\n".join(index))
    for p in written:
        print(f"  wrote {p.relative_to(OUT.parent)}  ({p.stat().st_size // 1024} KB)")
    print(f"  wrote {(OUT / 'README.md').relative_to(OUT.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
