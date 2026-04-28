PYTHON ?= python3
export PYTHONPATH := $(CURDIR)/src

.PHONY: test verify compile

test:
	$(PYTHON) -m unittest discover -s tests -v

verify:
	./scripts/verify_setup.sh

compile:
	$(PYTHON) -m compileall src scripts
