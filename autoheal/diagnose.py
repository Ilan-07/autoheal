"""DIAGNOSE: pick the repair. Memory first, then the deterministic ranker, then
-- only if it is still ambiguous -- a model.

The ordering is the point. By the time anything reaches the LLM, `localize` has
already produced eight candidates that were each *executed* against every record
on the page and scored on how many known-good values they reproduce. The model
is choosing between measured options, not reading HTML. That is why the prompt is
a few thousand tokens instead of a few hundred thousand, and why a bad model
answer costs a cycle rather than a wrong repair -- VERIFY still has to pass.

Note on determinism: `PLAN.md` specifies `temperature=0`. That parameter was
removed on Claude Opus 5 and now returns a 400, so it is not used. Reproducibility
of the *eval* does not depend on it -- the default diagnoser is deterministic and
the model is opt-in, precisely so the reported numbers can be re-run offline.
"""

from __future__ import annotations

import json
import os
from typing import Any

from pydantic import BaseModel, Field

from .diff import DomDiff, diff
from .localize import Candidate, candidates, evaluate_locator, strategy_of
from .memory import Episode, Fingerprint, Recall, Store
from .patch import SpecPatch
from .perceive import BreakageReport
from .spec import ExtractorSpec, Locator

# Which model, if any, backs the one step that is allowed to use one. Set via
# AUTOHEAL_LLM: "anthropic[:model]", "ollama:<model>", or unset for none.
# Unset is the default and is what every published number is produced with.
DEFAULT_ANTHROPIC = "claude-opus-5"
DEFAULT_OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
MAX_TOKENS = 2048
LLM_TIMEOUT = 300


def provider() -> tuple[str, str] | None:
    """Parse AUTOHEAL_LLM into (provider, model), or None when disabled."""
    spec = os.environ.get("AUTOHEAL_LLM", "").strip()
    if not spec or spec.lower() in ("none", "off", "0"):
        return None
    name, _, model = spec.partition(":")  # model names contain colons
    name = name.lower()
    if name == "anthropic":
        return "anthropic", model or DEFAULT_ANTHROPIC
    if name == "ollama":
        return "ollama", model or "qwen3.5:4b"
    return None


class Diagnosis(BaseModel):
    field: str
    fingerprint: Fingerprint
    candidates: list[Candidate] = Field(default_factory=list)
    recalls: list[Recall] = Field(default_factory=list)
    patch: SpecPatch | None = None
    used_memory: bool = False
    used_llm: bool = False
    # True when the ranker was not confident enough to decide alone, so a model
    # call *would* have been made. Recorded even when the model is switched off,
    # which is what lets the memory ablation be measured with no network at all:
    # a recalled episode that resolves the ambiguity shows up here as a call not
    # made, rather than as a token bill we would have to spend to observe.
    would_call_llm: bool = False
    # Set only when recall resolved a decision that was *genuinely* ambiguous.
    # Counting every memory hit here overstates the saving: a hit on a decision
    # the ranker would have made alone costs nothing to begin with.
    resolved_ambiguity: bool = False
    tokens: int = 0
    rationale: str = ""

    def cost_note(self) -> str:
        if self.used_memory:
            return "recalled from memory (0 tokens)"
        if self.used_llm:
            return f"model chose ({self.tokens} tokens)"
        return "deterministic top-1 (0 tokens)"


def diagnose(
    spec: ExtractorSpec,
    report: BreakageReport,
    field: str,
    *,
    broken_html: str,
    good_html: str,
    known_good: list[dict],
    store: Store | None = None,
    dom_diff: DomDiff | None = None,
    base_url: str = "",
    use_llm: bool = False,
    avoid: set[str] | None = None,
    cross_site_only: bool = False,
    regression_aware: bool = True,
    known_good_aware: bool = True,
) -> Diagnosis:
    """Produce one candidate patch for one field."""
    d = dom_diff or diff(good_html, broken_html)
    fp = Fingerprint.of(report, field, d.primary, spec)
    values = [r.get(field) for r in known_good]
    cands = candidates(spec, field, broken_html, values, old_html=good_html, base_url=base_url,
                       regression_aware=regression_aware, known_good_aware=known_good_aware)
    dx = Diagnosis(field=field, fingerprint=fp, candidates=cands)
    if not cands:
        dx.rationale = "no candidate locator reproduced any known-good value on the broken page"
        return dx

    avoid = avoid or set()
    live = [c for c in cands if _strategy(c) not in avoid] or cands
    ambiguous = len(live) > 1 and _ambiguous(live)

    if store is not None:
        dx.recalls = store.recall(fp, exclude_site=spec.site if cross_site_only else None)
        hit = _apply_recall(dx.recalls, live, known_good_aware)
        if hit is not None:
            dx.would_call_llm = False
            dx.resolved_ambiguity = ambiguous
            dx.used_memory = True
            dx.patch = SpecPatch(field=field, locator=hit.locator, strategy=_strategy(hit),
                                 reason=f"recalled: {dx.recalls[0].as_prior()}")
            dx.rationale = dx.recalls[0].as_prior()
            return dx

    dx.would_call_llm = ambiguous
    if use_llm and ambiguous:
        chosen, gen, note, tokens = _ask_model(spec, field, report, d, live)
        dx.used_llm, dx.tokens, dx.rationale = True, tokens, note
        if chosen is not None and gen:
            wider = evaluate_locator(
                spec, field, broken_html, values,
                chosen.locator.model_copy(update={"q": gen, "note": "model generalisation"}),
                old_html=good_html, base_url=base_url, regression_aware=regression_aware,
                known_good_aware=known_good_aware,
            )
            # Accept the widening only if it actually measures at least as well.
            if wider is not None and wider.score >= chosen.score:
                dx.rationale += f" | generalised to {gen!r} (verified: recovery {wider.recovery:.0%})"
                chosen = wider
            else:
                dx.rationale += f" | rejected generalisation {gen!r} (did not measure up)"
        if chosen is not None:
            dx.patch = SpecPatch(field=field, locator=chosen.locator, strategy=_strategy(chosen),
                                 reason=note or chosen.explain())
            return dx

    best = live[0]
    dx.patch = SpecPatch(field=field, locator=best.locator, strategy=_strategy(best), reason=best.explain())
    dx.rationale = dx.rationale or best.explain()
    return dx


def _strategy(c: Candidate) -> str:
    return f"adopt_candidate:{c.locator.kind}{':@' + c.locator.attr if c.locator.attr else ''}"


def _ambiguous(cands: list[Candidate]) -> bool:
    """Only spend a model call when the ranker is not already confident.

    A clear winner is a clear winner; paying Opus to agree with it is the kind of
    reflexive LLM call this design exists to avoid."""
    if len(cands) < 2:
        return False
    return (cands[0].score - cands[1].score) < 0.08 or cands[0].recovery < 0.99


def _apply_recall(recalls: list[Recall], cands: list[Candidate],
                  known_good_aware: bool = True) -> Candidate | None:
    """A prior repair only shortcuts the LLM if it matches something the ranker
    already validated on *this* page. Memory proposes; the runtime disposes.

    Matching is on the strategy *class*, not the concrete locator kind. Requiring
    the same kind (and the same attribute) meant a recalled repair could only fire
    on a page whose markup happened to resemble the original -- which is why
    cross-site recall was doing almost nothing."""
    for r in recalls:
        if r.episode.outcome != "healed":
            continue
        want = r.episode.strategy_class
        for c in cands:  # candidates are pre-sorted, so this takes the best match
            # The recovery check reads known-good values, so it is a leak path
            # in the -known-good arm: without this guard the ablation would
            # quietly get the supervision signal back through memory recall.
            if (known_good_aware and c.recovery < 0.99):
                continue
            if strategy_of(c.locator) == want:
                return c
    return None


# --- the model step -------------------------------------------------------

_SYSTEM = """You repair web-extraction specs. You are given a field that broke, \
evidence from a health monitor, a structural classification of what changed in the \
DOM, and a ranked list of candidate locators.

Every candidate has already been executed against every record on the live page. \
`recovery` is the fraction of records where it reproduced a value that the \
extractor is known to have produced correctly before the break; `coverage` is the \
fraction where it produced any valid value; `prior` is how robust that style of \
address is to future redesigns; `survives_old` means it also works on the \
pre-break snapshot.

Choose the candidate most likely to still be correct after the *next* redesign, \
not merely the one that scores highest today. Concretely: prefer semantic \
attributes and structured data over class names, prefer stable class names over \
hashed CSS-in-JS names, and prefer any of those over positional paths. Reject a \
candidate whose recovery is below 1.0 when a candidate with full recovery exists.

You may also propose a generalisation of the chosen candidate's query - for \
example widening `div.a > span.b` to `[itemprop=price]`. It will be re-executed \
and only used if it measures at least as well as the candidate you picked. Return \
null if you have no generalisation to offer."""

# Anthropic gets this as a strict tool schema. Ollama gets the same shape via its
# `format` parameter *and* spelled out in the prompt -- see `_JSON_INSTRUCTION`.
_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["candidate_index", "reason", "confidence", "generalized_q"],
    "properties": {
        "candidate_index": {"type": "integer", "description": "0-based index into the candidate list"},
        "reason": {"type": "string", "description": "One sentence, referring to the evidence."},
        "confidence": {"type": "number", "description": "0..1"},
        "generalized_q": {
            "type": ["string", "null"],
            "description": "A more robust query of the same locator kind, or null.",
        },
    },
}

_TOOL = {
    "name": "choose_locator",
    "description": "Choose which candidate locator should become the field's new primary.",
    "strict": True,
    "input_schema": _SCHEMA,
}

# Ollama's `format` is schema-*guided*, not constrained decoding -- their docs say
# so and recommend restating the schema in the prompt. Without this the model
# echoed the shape of the *input* candidates instead of the output schema, and
# every call was discarded by validation. With it, 5/5 calls parsed.
_JSON_INSTRUCTION = """

Respond with ONLY a JSON object having EXACTLY these four keys:
  {"candidate_index": <int>, "reason": <string>, "confidence": <number 0-1>, "generalized_q": <string or null>}
`candidate_index` is the index of the candidate you chose. Do not echo the candidate object itself."""


def _payload(spec, field, report, d: DomDiff, cands: list[Candidate]) -> dict:
    fspec = spec.fields[field]
    return {
        "site": spec.site,
        "field": field,
        "transform": fspec.transform,
        "validators": [v.model_dump(exclude_none=True) for v in fspec.validators],
        "current_stack": [l.signature() for l in fspec.stack],
        "health_evidence": report.evidence(field) if field in report.fields else [],
        "dom_diff": d.evidence(),
        "candidates": [
            {
                "index": i,
                "kind": c.locator.kind,
                "q": c.locator.q,
                "attr": c.locator.attr,
                "recovery": c.recovery,
                "coverage": c.coverage,
                "prior": c.prior,
                "survives_old": c.survives_old,
                "nodes_matched_per_record": c.n_hits_mean,
            }
            for i, c in enumerate(cands)
        ],
    }


def _call_anthropic(model: str, payload: dict) -> tuple[dict | None, int, str]:
    try:
        import anthropic
    except ImportError:
        return None, 0, "anthropic SDK not installed"
    try:
        resp = anthropic.Anthropic().messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            # Identical on every repair in a run, so it is the cache prefix;
            # only the payload below varies.
            system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": "choose_locator"},
            messages=[{"role": "user", "content": json.dumps(payload, indent=1)}],
        )
    except Exception as e:
        return None, 0, f"model call failed ({type(e).__name__})"
    tokens = resp.usage.input_tokens + resp.usage.output_tokens
    block = next((b for b in resp.content if b.type == "tool_use"), None)
    if block is None:
        return None, tokens, "model returned no tool call"
    return (block.input if isinstance(block.input, dict) else json.loads(block.input)), tokens, ""


def _call_ollama(model: str, payload: dict) -> tuple[dict | None, int, str]:
    """Local or Ollama-Cloud models, over plain HTTP -- no extra dependency."""
    import urllib.request

    body = json.dumps({
        "model": model,
        "stream": False,
        "format": _SCHEMA,
        "options": {"temperature": 0},  # available here, unlike on Opus 5
        "messages": [
            {"role": "system", "content": _SYSTEM + _JSON_INSTRUCTION},
            {"role": "user", "content": json.dumps(payload, indent=1)},
        ],
    }).encode()
    req = urllib.request.Request(
        f"{DEFAULT_OLLAMA_HOST}/api/chat", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        raw = json.loads(urllib.request.urlopen(req, timeout=LLM_TIMEOUT).read())
    except Exception as e:
        return None, 0, f"model call failed ({type(e).__name__})"
    tokens = int(raw.get("prompt_eval_count") or 0) + int(raw.get("eval_count") or 0)
    try:
        return json.loads(raw["message"]["content"]), tokens, ""
    except Exception:
        return None, tokens, "model returned unparseable JSON"


def _ask_model(
    spec, field, report, d: DomDiff, cands: list[Candidate]
) -> tuple[Candidate | None, str | None, str, int]:
    """Returns (chosen, proposed_generalisation, rationale, tokens).

    Every failure path returns the deterministic ranker's answer instead. A model
    that is missing, unreachable, slow, or wrong costs a cycle -- never a repair."""
    prov = provider()
    if prov is None:
        return None, None, "no model configured (AUTOHEAL_LLM unset); used the deterministic ranker", 0
    name, model = prov
    payload = _payload(spec, field, report, d, cands)
    caller = _call_anthropic if name == "anthropic" else _call_ollama
    args, tokens, err = caller(model, payload)
    if args is None:
        return None, None, f"{err}; fell back to the deterministic ranker", tokens

    idx = args.get("candidate_index")
    if not isinstance(idx, int) or isinstance(idx, bool) or not (0 <= idx < len(cands)):
        return None, None, f"model chose out-of-range candidate {idx!r}; fell back to the ranker", tokens
    chosen = cands[idx]
    note = f"[{name}:{model}] " + str(args.get("reason", ""))[:400]

    # The model may widen a query, but it does not get to assert that the wider
    # one works. It comes back as a string and the caller measures it.
    gen = args.get("generalized_q")
    gen = gen if isinstance(gen, str) and gen and gen != chosen.locator.q else None
    return chosen, gen, note, tokens
