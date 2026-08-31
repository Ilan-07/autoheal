"""Build the submission video: narrated slides over captured dashboard states.

Frame-based rather than a live screen recording, on purpose. `screencapture -v`
needs a macOS Screen Recording grant that must be given by hand, and it films
the whole desktop; capturing the dashboard state by state needs no permission,
leaks nothing else on screen, and is reproducible. The dashboard is a
step-through player anyway, so stills are the honest representation of it.

Two phases, because the middle step happens in a browser:

    python -m demo.make_video audio     # narration -> build/audio/NN.aiff  (+ frame plan)
    ... capture build/frames/NN.png, one per segment, at the given event index
    python -m demo.make_video assemble  # frames + audio -> demo/autoheal-demo.mp4

The narration is checked against `demo/events.json` at build time: every number
spoken has to match what the recorded run actually produced, or the build fails.
That check exists because a demo that narrates numbers the system did not
produce is precisely the failure this project is about.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
BUILD = HERE.parent / "build"
AUDIO, FRAMES = BUILD / "audio", BUILD / "frames"
OUT = HERE / "autoheal-demo.mp4"
VOICE, RATE = "Samantha", 178

# (event index to show, narration). The index is a state of demo/replay.html.
SEGMENTS: list[tuple[int, str]] = [
    (2, "This is a scraper that ran fine yesterday. Overnight, the site added a promoted "
        "item above every quote. You can see them on the left. Each one begins with the word "
        "Sponsored."),
    (2, "Here is what the scraper reports. Ten records. One hundred percent fill rate. Zero "
        "errors raised. Every dashboard you own is green."),
    (2, "And every value is wrong. It is reading Sponsored, Albert Einstein, where the author "
        "is Albert Einstein. F one against ground truth is zero point three three. Nothing "
        "threw an exception. Nothing retried. A pipeline like this writes garbage for three "
        "weeks and nobody finds out."),
    (7, "Autoheal does not wait for an exception, because there is not one. It compares this "
        "run against a baseline of healthy runs. The signal that fires hardest is match count. "
        "The locator now matches two nodes per record where it used to match one. The runtime "
        "already knew that, and was throwing it away."),
    (10, "It then takes yesterday's known good values and finds them in today's page. Eight "
         "candidate locators, and each one is actually executed against every record. Not "
         "guessed. Measured. How many known values it recovers, how robust the addressing "
         "style is, and whether it still works on the page that worked before."),
    (11, "The winner excludes the impostor by the class it carries and the real node does not. "
         "That is the fix a human would write. No marker name appears anywhere in the repair "
         "code. It is read off the page."),
    (16, "Three gates, all mandatory. Recovery. Regression, which re-runs the patched spec "
         "against the page that still worked, and is what separates a repair from an overfit. "
         "And clearance. The monitor has to go quiet."),
    (18, "F one goes from zero point three three to one point zero, in one cycle, using zero "
         "tokens. No model was called. The deterministic ranker settled it."),
    (17, "And look at the patch. The new locator goes in at tier zero, and the old one is "
         "demoted, not deleted, because sites A B test and revert."),
    (22, "Now a different site, a shop, with the same class of breakage. Note the marker is "
         "different here. Nothing is hardcoded. Memory holds two episodes, both from the quotes "
         "site. Nothing from this one."),
    (32, "Same break, run twice. Without memory, two model calls and {ablated_tokens} tokens. "
         "With the episode the other site left behind, zero calls and zero tokens. Same repair, "
         "same F one. Memory does not make it smarter. It makes it cheaper."),
    (32, "Across six sites and thirty breakages. A static scraper recovers zero. A one shot "
         "language model, given the whole page, recovers zero point six three. Autoheal "
         "recovers zero point eight seven, significant at p equals zero point zero one six."),
    (32, "The one shot baseline burned five hundred and ninety thousand tokens, and thirty six "
         "percent of the locators it chose do not work on the page that worked yesterday. "
         "Autoheal used zero tokens and produced no overfits."),
    (32, "Ten of the eleven modules never call a model. That is the point. The agent is the "
         "small, constrained part of a mostly deterministic system."),
]

# Spoken claims that must match the recorded run, so narration cannot drift.
def _check_against_recording() -> list[str]:
    ev = json.loads((HERE / "events.json").read_text())["events"]
    static = next(e for e in ev if e["kind"] == "static")
    ab = next(e for e in ev if e["kind"] == "ab")
    res = next(e for e in ev if e["kind"] == "result")
    bad = []
    if (static["records"], static["fill"], round(static["f1"], 2)) != (10, 1.0, 0.33):
        bad.append(f"narration says 10 records / 100% fill / F1 0.33; recording has "
                   f"{static['records']} / {static['fill']:.0%} / {static['f1']:.2f}")
    if not static["silent"]:
        bad.append("narration calls it a silent failure; the recording does not")
    if (res["f1_before"], res["f1_after"], res["llm_calls"]) != (0.3333, 1.0, 0):
        bad.append(f"narration says 0.33 -> 1.0 in 0 calls; recording has "
                   f"{res['f1_before']} -> {res['f1_after']} in {res['llm_calls']}")
    if (ab["with_memory"]["calls"], ab["ablated"]["calls"]) != (0, 2):
        bad.append(f"narration says 0 vs 2 calls; recording has "
                   f"{ab['with_memory']['calls']} vs {ab['ablated']['calls']}")
    return bad


def _spoken_numbers() -> dict[str, str]:
    """Numbers the narration reads straight out of the recording.

    The token count moves between recordings because a model produces it, and a
    hardcoded "three thousand seven hundred" went stale the first time the demo
    was re-recorded -- caught by the drift check, which is the point. Deriving it
    means the narration cannot disagree with the artefact it is describing."""
    ev = json.loads((HERE / "events.json").read_text())["events"]
    ab = next(e for e in ev if e["kind"] == "ab")
    n = ab["ablated"]["tokens"]
    return {"ablated_tokens": f"about {round(n / 100) / 10:.1f} thousand".replace(".0 ", " ")}


def _dur(path: pathlib.Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return float(out.stdout.strip())


def build_audio() -> int:
    drift = _check_against_recording()
    if drift:
        print("NARRATION DOES NOT MATCH THE RECORDING:")
        for d in drift:
            print(f"  - {d}")
        print("\nRe-record with `make demo`, or fix the script. Refusing to build.")
        return 1
    AUDIO.mkdir(parents=True, exist_ok=True)
    FRAMES.mkdir(parents=True, exist_ok=True)
    total = 0.0
    plan = []
    for i, (idx, text) in enumerate(SEGMENTS):
        aiff = AUDIO / f"{i:02d}.aiff"
        text = text.format(**_spoken_numbers())
        subprocess.run(["say", "-v", VOICE, "-r", str(RATE), "-o", str(aiff), text], check=True)
        d = _dur(aiff)
        total += d
        plan.append({"segment": i, "event_index": idx, "seconds": round(d, 2),
                     "frame": str(FRAMES / f"{i:02d}.png")})
        print(f"  {i:02d}  event {idx:2}  {d:5.1f}s  {text[:58]}...")
    (BUILD / "plan.json").write_text(json.dumps(plan, indent=1))
    print(f"\n  {len(SEGMENTS)} segments, {total/60:.1f} min total (brief allows 5)")
    print(f"  now capture one PNG per segment into {FRAMES}/NN.png at the event index shown")
    return 0


def assemble() -> int:
    plan = json.loads((BUILD / "plan.json").read_text())
    missing = [p["segment"] for p in plan if not pathlib.Path(p["frame"]).exists()]
    if missing:
        print(f"missing frames for segments {missing} — capture them first")
        return 1

    # One concat list for stills, one for audio; ffmpeg muxes them at the end.
    vlist = BUILD / "video.txt"
    alist = BUILD / "audio.txt"
    vlist.write_text("".join(
        f"file '{p['frame']}'\nduration {p['seconds']}\n" for p in plan)
        + f"file '{plan[-1]['frame']}'\n")  # concat needs the last frame repeated
    alist.write_text("".join(
        "file '%s'\n" % (AUDIO / ("%02d.aiff" % p["segment"])) for p in plan))
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(alist),
                    "-c:a", "aac", "-b:a", "160k", str(BUILD / "narration.m4a")], check=True,
                   capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(vlist),
                    "-i", str(BUILD / "narration.m4a"),
                    "-vf", "scale=1600:-2:flags=lanczos,format=yuv420p",
                    "-r", "30", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                    "-c:a", "copy", "-shortest", str(OUT)], check=True, capture_output=True)
    print(f"  wrote {OUT}  ({OUT.stat().st_size / 1_048_576:.1f} MB, {_dur(OUT):.0f}s)")
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "audio"
    raise SystemExit(build_audio() if mode == "audio" else assemble())
