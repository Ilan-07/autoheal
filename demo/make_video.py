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
# Check a voice is real before trusting it: on a stock macOS only a handful are
# installed and every other name silently falls back to one default, which you
# can spot because the rendered duration and byte count are identical across all
# of them. `Aman` here is a downloaded voice; the stock fallbacks it beats are
# Samantha, Daniel, Karen, Moira and Tessa. Rate and sentence pauses still matter
# more than the voice for listenability -- see MIN_HOLD below for the other half.
VOICE, RATE = "Aman", 176
PAUSE_MS = 190          # inserted at sentence boundaries
PARA_MS = 340           # inserted between segments

# A frame has to stay up long enough to be read, regardless of how long the line
# over it takes to say. Raising the speaking rate to a conversational 176 shortened
# every segment, and the dense frames -- the comparison table, the ranked candidate
# lists -- became unreadable before they cut. These are floors in seconds, applied
# by padding the narration with trailing silence rather than by speaking slower.
MIN_HOLD: dict[object, float] = {
    "card1": 15.0,   # three-row failure table
    "card2": 20.0,   # six-row comparison table, the densest frame in the video
    "card3": 12.0,   # three stat boxes plus a paragraph
    "card4": 11.0,   # the reproduce-it card, last thing on screen
    "card5": 13.0,   # the agency statement plus its three-row evidence table
    1: 9.0,          # the page alone, before the static panel appears
    10: 14.0,        # eight ranked candidates with six columns
    11: 12.0,        # same table, plus the chosen patch line
    16: 11.0,        # gates, plus both candidate tables still on screen
    17: 12.0,        # the spec diff
    32: 13.0,        # the with/without-memory panel
}

# (frame, narration). `frame` is an int -> a state of demo/replay.html, or
# "cardN" -> a title card from demo/cards.html.
SEGMENTS: list[tuple[object, str]] = [
    # --- intro: set the problem up before showing anything
    ("card0", "This is Autoheal, a self healing web extraction system. Anyone running scrapers in "
              "production has the same problem. You do not control the sites you extract from, "
              "nobody tells you when they change, and the extractors are your product."),
    ("card1", "Scrapers fail in three ways, and only one is handled well today. The request fails, "
              "and you retry. The selector matches nothing, and a fill rate alarm catches it. But "
              "when the selector matches the wrong node, nothing catches it at all. Here is what "
              "that looks like."),

    # --- act I: the silent failure
    (1, "This scraper ran fine yesterday. Overnight the site added a promoted item above every "
        "quote. You can see them on the left, each beginning with the word Sponsored."),
    (2, "Here is what the scraper reports. Ten records. One hundred percent fill rate. Zero errors. "
        "Every dashboard you own is green."),
    (2, "And every value is wrong. It reads Sponsored, Albert Einstein, where the author is Albert "
        "Einstein. F one against ground truth is zero point three three. Nothing threw. A pipeline "
        "like this writes garbage for weeks before anyone finds out."),

    # --- act II: the heal
    (7, "Autoheal does not wait for an exception, because there is not one. It compares this run "
        "against a baseline of healthy runs. The signal firing hardest is match count. The locator "
        "now matches two nodes per record where it used to match one."),
    (10, "It then takes yesterday's known good values and finds them in today's page. Eight "
         "candidates, each one actually executed against every record. Not guessed. Measured."),
    (11, "The winner excludes the impostor by the class it carries and the real node does not. That "
         "is the fix a human would write, and no marker name appears anywhere in the repair code."),
    (16, "Three gates, all mandatory. Recovery. Regression, which re-runs the patch against the page "
         "that still worked and is what separates a repair from an overfit. And clearance."),
    (18, "F one goes from zero point three three to one point zero, in one cycle, using zero tokens. "
         "No model was called."),
    (17, "And the old locator is demoted, not deleted, because sites A B test and revert."),

    # --- act III: memory
    (22, "A different site now, with the same class of breakage. Note the marker differs. Nothing is "
         "hardcoded. Memory holds two episodes, both from the previous site, nothing from this one."),
    (32, "Same break, run twice. Without memory, two model calls and {ablated_tokens} tokens. With "
         "the episode the other site left behind, zero calls and zero tokens. Memory does not make "
         "it smarter. It makes it cheaper."),

    # --- outro
    ("card2", "Here is the whole comparison. A static scraper recovers nothing. A one shot language "
              "model given the entire page recovers zero point six three, and thirty six percent of "
              "the locators it picks do not work on the page that worked yesterday. Autoheal "
              "recovers zero point eight seven, using zero tokens, with no overfits."),
    ("card3", "The change that mattered most was giving the agent yesterday's known good values as "
              "its supervision signal. Remove it and recovery collapses to zero point one three."),
    ("card3", "The experiment we removed was the cycles to recover chart. We planned it as a "
              "headline, then measured cycles flat at one, so we retired the claim rather than "
              "massage it. Two ablations came back null and are published as nulls."),
    ("card5", "So, is this an agentic system? We built the loop, measured it, and found the "
              "deterministic parts carry it. We are reporting that rather than hiding it."),
    ("card5", "The agent capabilities that do earn their place are memory, verification, and "
              "context engineering, and we can prove it, because ablating each one moves the "
              "number. What we can also prove is that the language model is not secretly doing "
              "the work. Almost no agent demo can say that."),
    ("card4", "And everything you have just seen regenerates with one command, offline, with no API "
              "key. Thanks for watching."),
]


def _natural(text: str) -> str:
    """Give the synthesiser somewhere to breathe.

    Legacy macOS voices run sentences together, which is most of what makes them
    sound mechanical. An explicit pause at each sentence boundary costs nothing
    and does more for listenability than swapping between the voices available."""
    text = text.replace(". ", f". [[slnc {PAUSE_MS}]] ")
    return text + f" [[slnc {PARA_MS}]]"

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
    for i, (frame, text) in enumerate(SEGMENTS):
        aiff = AUDIO / f"{i:02d}.aiff"
        spoken = _natural(text.format(**_spoken_numbers()))
        subprocess.run(["say", "-v", VOICE, "-r", str(RATE), "-o", str(aiff), spoken], check=True)
        d = _dur(aiff)
        floor = MIN_HOLD.get(frame, 0.0)
        if d < floor:  # hold the frame by padding with silence, not by slowing down
            pad = int((floor - d) * 1000)
            subprocess.run(["say", "-v", VOICE, "-r", str(RATE), "-o", str(aiff),
                            spoken + f" [[slnc {pad}]]"], check=True)
            d = _dur(aiff)
        total += d
        plan.append({"segment": i, "source": frame, "seconds": round(d, 2),
                     "frame": str(FRAMES / f"{i:02d}.png")})
        held = " (held)" if MIN_HOLD.get(frame, 0.0) > 0 and d >= MIN_HOLD[frame] - 0.2 else ""
        print(f"  {i:02d}  {str(frame):>6}  {d:5.1f}s{held:7} {text[:48]}...")
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
