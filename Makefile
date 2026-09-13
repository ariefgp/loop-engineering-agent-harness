.PHONY: test shadow

test:
	python3 -m unittest discover -s tests -v

shadow:
	python3 -m loop_harness tick --dry-run
