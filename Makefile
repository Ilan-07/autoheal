.PHONY: eval test truth verify all
all: verify test eval
eval:    ## B0 static baseline over every site x mutation
	uv run python -m eval.harness --seed 0
test:
	uv run pytest -q
verify:  ## independent ground-truth checks (not the tautological F1 test)
	uv run python -m eval.verify_truth
truth:   ## re-freeze ground truth from the v1 specs, then re-verify
	uv run python eval/author_specs.py && uv run python -m eval.freeze_truth && $(MAKE) verify
