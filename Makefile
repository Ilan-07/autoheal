.PHONY: eval test truth verify check perceive heal check-heal ablations b1 trajectories demo demo-replay all
all: verify test check perceive heal check-heal

test:
	uv run pytest -q
verify:  ## independent ground-truth checks (not the tautological F1 test)
	uv run python -m eval.verify_truth
eval:    ## B0 static baseline over every site x mutation
	uv run python -m eval.harness --seed 0
check:   ## fail if the committed B0 baseline no longer reproduces
	uv run python -m eval.check_baseline
perceive:  ## breakage detection rate vs false-alarm rate (fails on either)
	uv run python -m eval.perceive_eval --seed 0
heal:      ## end-to-end recovery, ranker accuracy, memory ablation
	uv run python -m eval.heal_eval --seed 0
b1:          ## B1 one-shot baseline (needs AUTOHEAL_LLM-capable model; NOT reproducible)
	uv run python -m eval.b1_oneshot --seed 0
ablations:   ## which parts of the design are load-bearing (incl. null results)
	uv run python -m eval.ablations --seed 0
check-heal:  ## fail if the committed healing numbers no longer reproduce
	uv run python -m eval.check_heal
trajectories: ## regenerate the four agent trajectories from live runs
	uv run python -m eval.trajectories
demo:        ## re-record the demo from real runs (set AUTOHEAL_LLM for real token counts)
	uv run python -m demo.record
demo-replay: ## open the recorded demo -- self-contained, offline, no server
	open demo/replay.html
truth:   ## re-freeze ground truth from the v1 specs, then re-verify
	uv run python eval/author_specs.py && uv run python -m eval.freeze_truth && $(MAKE) verify
