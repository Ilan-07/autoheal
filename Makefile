.PHONY: eval test truth verify check all
all: verify test check eval
eval:    ## B0 static baseline over every site x mutation
	uv run python -m eval.harness --seed 0
test:
	uv run pytest -q
verify:  ## independent ground-truth checks (not the tautological F1 test)
	uv run python -m eval.verify_truth
check:   ## fail if the committed B0 baseline no longer reproduces
	uv run python -m eval.check_baseline
truth:   ## re-freeze ground truth from the v1 specs, then re-verify
	uv run python eval/author_specs.py && uv run python -m eval.freeze_truth && $(MAKE) verify
