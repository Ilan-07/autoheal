# Agent trajectories

Representative end-to-end runs for every agent in this project, captured live by
`uv run python -m eval.trajectories`. Prompts and replies are verbatim.

| trajectory | agent | what it shows |
|---|---|---|
| [01 — silent failure, healed](01-silent-failure-healed.md) | Autoheal repair loop | detection of a silent failure, repair, three gates, additive patch |
| [02 — memory transfers](02-memory-transfers-across-sites.md) | Autoheal repair loop | recall from another site resolving a decision for zero tokens |
| [03 — quarantine](03-quarantine-human-checkpoint.md) | Autoheal repair loop | an honest refusal and the human-review card |
| [04 — baseline B1](04-baseline-b1-one-shot.md) | One-shot baseline agent | the same repair from raw HTML, for comparison |

The loop is deterministic apart from the model step, which is reached on roughly
one decision in six; the other five are settled by memory recall or the ranker.
