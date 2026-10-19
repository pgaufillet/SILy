# SILy: host tests, lint, firmware and deployment.
#
# Tools come from the t3s3-micropython development environment: MicroPython
# source tree, ESP-IDF, unix port binary (`make unix` there) and a venv with
# mpremote, esptool, ruff and codespell (`make venv` there).

T3S3 ?= $(HOME)/git/t3s3-micropython
MICROPYTHON ?= $(T3S3)/micropython/ports/unix/build-standard/micropython
MPY_LIB ?= $(T3S3)/micropython/lib/micropython-lib
VENV ?= $(T3S3)/.venv/mpremote
MPREMOTE ?= $(VENV)/bin/mpremote
RUFF ?= $(VENV)/bin/ruff
CODESPELL ?= $(VENV)/bin/codespell

# Serial device of the board for flash and deploy.
PORT ?= /dev/ttyACM0

# Firmware variant built out of tree by the t3s3-micropython Makefile.
BOARD = SILY_T3S3
BOARD_DIR = $(CURDIR)/firmware/$(BOARD)

# Host tests: application sources, the host-only simulator (sim/, from the
# repository root) and micropython-lib's unittest, without installing anything
# into the MicroPython tree.
TEST_PATH = .frozen:$(CURDIR)/src:$(CURDIR):$(MPY_LIB)/python-stdlib/unittest
# Heap of the unix port for host tests (default 2 MB): simulated hours of
# multi-node traffic keep their traces in memory.
TEST_HEAP ?= 32M

# TLA+ models of spec/ (TLC from tla2tools.jar, Java 11 or later).
TLA_JAR ?= $(HOME)/.local/lib/tla2tools.jar

# Top-level entries of src/ copied to the board's filesystem root.
DEPLOY_ITEMS = $(notdir $(wildcard src/*))

LINT_PATHS = src sim tests tools docs firmware Makefile README.md

# Runs of the scenario suites for `make seeds` (seed shifts 0 to SEEDS - 1).
SEEDS ?= 24

# Simulator benchmark (`make simbench`): grid of NODES boards for MINUTES
# simulated minutes from SEED.
NODES ?= 30
MINUTES ?= 10
SEED ?= 1

# Large-network suite (`make large`, tests/large.py): every scenario and
# topology at 30 nodes on LARGE_SEEDS seeds, plus one 64-node run per
# scenario, LARGE_JOBS runs at a time; RUN="SCENARIO TOPOLOGY SEED [NODES]"
# runs one of them alone with its full report.
LARGE_SEEDS ?= 3
LARGE_JOBS ?= 4
RUN ?=
LARGE = env MICROPYPATH=$(TEST_PATH) $(MICROPYTHON) -X heapsize=$(TEST_HEAP) tests/large.py

.PHONY: help check test seeds simbench large lint firmware flash deploy spec spec-pass spec-defects

help:
	@echo "make test                 host tests on the MicroPython unix port"
	@echo "make seeds [SEEDS=24]     scenario suites on SEEDS seeds (not part of check)"
	@echo "make simbench             simulator benchmark: wall clock, digest (NODES, MINUTES, SEED)"
	@echo "make large [LARGE_SEEDS=3] large-network invariants (not part of check); RUN=\"...\": one run"
	@echo "make lint                 ruff (check, format) and codespell"
	@echo "make check                lint + test"
	@echo "make firmware             build the $(BOARD) firmware"
	@echo "make flash PORT=<dev>     flash the $(BOARD) firmware"
	@echo "make deploy PORT=<dev>    copy src/ to the board and reset it"
	@echo "make spec                 TLC: every model, each against its expected outcome"
	@echo "make spec-pass            TLC: the models that must hold"
	@echo "make spec-defects         TLC: the models that must fail (defects, limits)"

check: lint test

test:
	MICROPYPATH=$(TEST_PATH) $(MICROPYTHON) -X heapsize=$(TEST_HEAP) tests/run.py tests

seeds:
	MICROPYPATH=$(TEST_PATH) $(MICROPYTHON) -X heapsize=$(TEST_HEAP) tests/seeds.py $(SEEDS)

simbench:
	MICROPYPATH=$(TEST_PATH) $(MICROPYTHON) -X heapsize=$(TEST_HEAP) tools/simbench.py $(NODES) $(MINUTES) $(SEED)

large:
ifeq ($(strip $(RUN)),)
	@dir=$$(mktemp -d) && start=$$(date +%s) && \
	$(LARGE) plan $(LARGE_SEEDS) | xargs -P $(LARGE_JOBS) -L 1 $(LARGE) run $$dir; \
	$(LARGE) summary $$dir $$(($$(date +%s) - start)); status=$$?; rm -rf $$dir; exit $$status
else
	$(LARGE) run - $(RUN)
endif

lint:
	$(RUFF) check .
	$(RUFF) format --check .
	$(CODESPELL) $(LINT_PATHS)

firmware:
	$(MAKE) -C $(T3S3) build BOARD=$(BOARD) BOARD_DIR=$(BOARD_DIR)

flash:
	$(MAKE) -C $(T3S3) flash BOARD=$(BOARD) BOARD_DIR=$(BOARD_DIR) PORT=$(PORT)

deploy:
	cd src && $(MPREMOTE) connect $(PORT) resume fs cp -r $(DEPLOY_ITEMS) : + reset

spec:
	TLA_JAR=$(TLA_JAR) sh spec/run.sh all

spec-pass:
	TLA_JAR=$(TLA_JAR) sh spec/run.sh pass

spec-defects:
	TLA_JAR=$(TLA_JAR) sh spec/run.sh defects
