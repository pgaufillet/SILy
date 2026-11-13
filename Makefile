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

# TLA+ models of spec/ (TLC from tla2tools.jar, Java 11 or later), run by
# spec/run.sh: JOBS configurations at a time, each with WORKERS TLC workers
# and a Java heap of HEAP (-Xmx; with JOBS > 1 both default to a share of
# the processors and memory), MAXSECS a time limit per configuration, the
# quick tier the configurations recorded in spec/durations.txt at most
# QUICK_SECS seconds. NICE prefixes local runs (NICE="nice -n 19").
TLA_JAR ?= $(HOME)/.local/lib/tla2tools.jar
JOBS ?= 1
WORKERS ?=
HEAP ?=
MAXSECS ?=
QUICK_SECS ?= 60
NICE ?=
SPEC = $(NICE) env TLA_JAR=$(TLA_JAR) JOBS=$(JOBS) WORKERS=$(WORKERS) HEAP=$(HEAP) \
	MAXSECS=$(MAXSECS) QUICK_SECS=$(QUICK_SECS) sh spec/run.sh

# Full tier on a remote Linux machine (spec/remote.sh): SPEC_HOST=user@host,
# SPEC_DIR the remote directory, SPEC_JOBS configurations at a time there,
# SPEC_NAMES what to run (all, quick or configuration names), the results
# fetched into SPEC_FETCH.
SPEC_HOST ?=
SPEC_DIR ?= sily-spec
SPEC_JOBS ?= 8
SPEC_NAMES ?= all
SPEC_FETCH ?= build/spec-remote
SPEC_REMOTE = env TLA_JAR=$(TLA_JAR) REMOTE_DIR=$(SPEC_DIR) FETCH=$(SPEC_FETCH) \
	JOBS=$(SPEC_JOBS) WORKERS=$(WORKERS) HEAP=$(HEAP) MAXSECS=$(MAXSECS) \
	QUICK_SECS=$(QUICK_SECS) sh spec/remote.sh

# Top-level entries of src/ copied to the board's filesystem root.
DEPLOY_ITEMS = $(notdir $(wildcard src/*))

LINT_PATHS = src sim tests tools docs firmware Makefile README.md

# Scenario suites on several seeds (`make seeds`, tests/seeds.py): seed
# shifts SEEDS_FIRST to SEEDS_FIRST + SEEDS - 1 in chunks of SEEDS_CHUNK
# shifts, SEEDS_JOBS chunks at a time, their JSON lines into SEEDS_OUT, then
# the failure rates per test class and each failing (shift, class, test)
# with the command reproducing it alone; fails on a failing, missing or
# crashed shift. SEEDS_CLASS: only these test classes (CLASS or
# MODULE.CLASS). SEEDS_JOBS=1: the shifts in turn, with the unittest output.
SEEDS ?= 500
SEEDS_FIRST ?= 0
SEEDS_JOBS ?= 8
SEEDS_CHUNK ?= 5
SEEDS_CLASS ?=
SEEDS_OUT ?= build/seeds
SEEDS_PY = env MICROPYPATH=$(TEST_PATH) $(MICROPYTHON) -X heapsize=$(TEST_HEAP) tests/seeds.py
SEEDS_OPTIONS = $(foreach c,$(SEEDS_CLASS),--class $(c))
SEEDS_JOB = $(SEEDS_PY) "$$2" --first "$$1" --json $(SEEDS_OPTIONS)

# Simulator benchmark (`make simbench`): grid of NODES boards for MINUTES
# simulated minutes from SEED.
NODES ?= 30
MINUTES ?= 10
SEED ?= 1

# Large-network suite (`make large`, tests/large.py): every scenario and
# topology at 30 nodes on LARGE_SEEDS seeds, plus one 64-node run per
# scenario, LARGE_JOBS runs at a time; RUN="SCENARIO TOPOLOGY SEED [NODES
# [LOAD]]" runs one of them alone with its full report (TOPOLOGY: domain,
# star, star-domain, star2b, geometric, or for single runs grid, course: at
# most 14 nodes; LOAD: s15, the default, steady or idle). The star runs at
# s15 are overload cases: result "overload" when they pass on their
# recovery criterion.
LARGE_SEEDS ?= 3
LARGE_JOBS ?= 4
RUN ?=
LARGE = env MICROPYPATH=$(TEST_PATH) $(MICROPYTHON) -X heapsize=$(TEST_HEAP) tests/large.py

# Sweeps of the large-network suite (`make sweep`): the runs of `large.py
# sweep-plan $(SWEEP)` (e.g. SWEEP="--seeds 1-300 --topologies star
# --loads steady"), SWEEP_JOBS at a time, each recorded into SWEEP_OUT
# (a run already recorded there is skipped: an interrupted sweep resumes),
# then their failure rates with 95 % Wilson intervals; SWEEP_VARIANT and
# SWEEP_REV label the records. On a rented machine (tools/sweep_remote.sh):
# SWEEP_HOST=user@host, SWEEP_DIR the remote directory, SWEEP_REMOTE_JOBS
# runs at a time there, SWEEP_TREES further source trees as LABEL=DIR,
# SWEEP_KIND large or seeds (then SWEEP: seeds.py classes and modules,
# SWEEP_SHIFTS the shifts FIRST-LAST, SWEEP_CHUNK shifts per job), the
# records fetched into SWEEP_FETCH.
SWEEP ?=
SWEEP_JOBS ?= 4
SWEEP_OUT ?= build/sweep
SWEEP_VARIANT ?= current
SWEEP_REV ?= $(shell git describe --always --dirty --exclude='*' 2>/dev/null)
SWEEP_HOST ?=
SWEEP_DIR ?= sily-sweep
SWEEP_REMOTE_JOBS ?= 32
SWEEP_TREES ?=
SWEEP_KIND ?= large
SWEEP_SHIFTS ?= 0-99
SWEEP_CHUNK ?= 10
SWEEP_FETCH ?= build/sweep-remote
SWEEP_REMOTE = env MICROPYTHON=$(MICROPYTHON) MPY_LIB=$(MPY_LIB) REMOTE_DIR=$(SWEEP_DIR) \
	FETCH=$(SWEEP_FETCH) JOBS=$(SWEEP_REMOTE_JOBS) KIND=$(SWEEP_KIND) SWEEP="$(SWEEP)" \
	SHIFTS=$(SWEEP_SHIFTS) CHUNK=$(SWEEP_CHUNK) sh tools/sweep_remote.sh

# Trace recording (`make trace`, tools/trace.py, sim/trace.py): TEST=MODULE[.CLASS]
# records every network a test class of tests/ builds, RUN="SCENARIO TOPOLOGY
# SEED [NODES [LOAD]]" one run of the large-network suite; JSON Lines files into OUT.
TEST ?=
OUT ?= build/trace
TRACE = env MICROPYPATH=$(TEST_PATH) $(MICROPYTHON) -X heapsize=$(TEST_HEAP) tools/trace.py $(OUT)

.PHONY: help check test seeds simbench large sweep sweep-summary sweep-remote \
	sweep-remote-fetch trace lint firmware flash deploy \
	spec spec-quick spec-pass spec-defects spec-calibrate spec-remote spec-remote-fetch

help:
	@echo "make test                 host tests on the MicroPython unix port"
	@echo "make seeds [SEEDS=500 SEEDS_JOBS=8]  scenario suites on SEEDS seed shifts (not in check)"
	@echo "make simbench             simulator benchmark: wall clock, digest (NODES, MINUTES, SEED)"
	@echo "make large [LARGE_SEEDS=3] large-network invariants (not part of check); RUN=\"...\": one run"
	@echo "make sweep SWEEP=\"<sweep-plan options>\" [SWEEP_JOBS=4 SWEEP_OUT=build/sweep]  failure rates"
	@echo "make sweep-summary [SWEEP_OUT=build/sweep]  the failure rates of the records there"
	@echo "make sweep-remote SWEEP_HOST=user@host SWEEP=\"...\" [SWEEP_TREES=\"LABEL=DIR ...\"]  start there"
	@echo "make sweep-remote-fetch SWEEP_HOST=user@host  its progress; when done, the failure rates"
	@echo "make trace TEST=M[.C]|RUN=\"...\" record traces of a test class or a large run [OUT=build/trace]"
	@echo "make lint                 ruff (check, format), codespell, no logbook references"
	@echo "make check                lint + test"
	@echo "make firmware             build the $(BOARD) firmware"
	@echo "make flash PORT=<dev>     flash the $(BOARD) firmware"
	@echo "make deploy PORT=<dev>    copy src/ to the board and reset it"
	@echo "make spec                 TLC: every model, each against its expected outcome (full tier)"
	@echo "make spec-quick           TLC: the models recorded at most QUICK_SECS=60 s (quick tier)"
	@echo "make spec-pass            TLC: the models that must hold"
	@echo "make spec-defects         TLC: the models that must fail (defects, limits)"
	@echo "make spec-calibrate       TLC: every model with MAXSECS=QUICK_SECS, to record durations"
	@echo "  spec options: JOBS=1 WORKERS HEAP MAXSECS QUICK_SECS=60 NICE=\"nice -n 19\""
	@echo "make spec-remote SPEC_HOST=user@host  start the full tier there, detached [SPEC_JOBS=8]"
	@echo "make spec-remote-fetch SPEC_HOST=user@host  its state; when done, logs and durations"

check: lint test

test:
	MICROPYPATH=$(TEST_PATH) $(MICROPYTHON) -X heapsize=$(TEST_HEAP) tests/run.py tests

seeds:
ifeq ($(strip $(SEEDS_JOBS)),1)
	$(SEEDS_PY) $(SEEDS) --first $(SEEDS_FIRST) $(SEEDS_OPTIONS)
else
	@mkdir -p $(SEEDS_OUT) && rm -f $(SEEDS_OUT)/seeds_*.jsonl && start=$$(date +%s) && \
	$(SEEDS_PY) plan $(SEEDS) $(SEEDS_CHUNK) --first $(SEEDS_FIRST) | xargs -P $(SEEDS_JOBS) -L 1 \
		sh -c '$(SEEDS_JOB) >$(SEEDS_OUT)/seeds_$$1.jsonl' seeds; \
	$(SEEDS_PY) aggregate --expect $(SEEDS_FIRST)-$$(($(SEEDS_FIRST) + $(SEEDS) - 1)) \
		$(SEEDS_OUT); status=$$?; echo "seeds: $$(($$(date +%s) - start)) s"; exit $$status
endif

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

sweep:
	@mkdir -p $(SWEEP_OUT) && $(LARGE) sweep-plan $(SWEEP) >$(SWEEP_OUT)/plan.lst || exit 2; \
	SWEEP_VARIANT=$(SWEEP_VARIANT) SWEEP_REV=$(SWEEP_REV) \
		xargs -P $(SWEEP_JOBS) -L 1 $(LARGE) record $(SWEEP_OUT) <$(SWEEP_OUT)/plan.lst; \
	status=$$?; $(LARGE) aggregate $(SWEEP_OUT) || exit; exit $$status

sweep-summary:
	$(LARGE) aggregate $(SWEEP_OUT)

sweep-remote:
	@test -n "$(SWEEP_HOST)" || { echo "make sweep-remote SWEEP_HOST=user@host"; exit 2; }
	$(SWEEP_REMOTE) start $(SWEEP_HOST) $(SWEEP_TREES)

sweep-remote-fetch:
	@test -n "$(SWEEP_HOST)" || { echo "make sweep-remote-fetch SWEEP_HOST=user@host"; exit 2; }
	$(SWEEP_REMOTE) fetch $(SWEEP_HOST)

trace:
ifneq ($(strip $(TEST)),)
	$(TRACE) test $(TEST)
else ifneq ($(strip $(RUN)),)
	$(TRACE) large $(RUN)
else
	@echo 'make trace TEST=MODULE[.CLASS] or RUN="SCENARIO TOPOLOGY SEED [NODES [LOAD]]"'; exit 2
endif

lint:
	$(RUFF) check .
	$(RUFF) format --check .
	$(CODESPELL) $(LINT_PATHS)
	sh tools/public_text.sh

firmware:
	$(MAKE) -C $(T3S3) build BOARD=$(BOARD) BOARD_DIR=$(BOARD_DIR)

flash:
	$(MAKE) -C $(T3S3) flash BOARD=$(BOARD) BOARD_DIR=$(BOARD_DIR) PORT=$(PORT)

deploy:
	cd src && $(MPREMOTE) connect $(PORT) resume fs cp -r $(DEPLOY_ITEMS) : + reset

spec:
	$(SPEC) all

spec-quick:
	$(SPEC) quick

spec-pass:
	$(SPEC) pass

spec-defects:
	$(SPEC) defects

spec-calibrate:
	$(NICE) env TLA_JAR=$(TLA_JAR) JOBS=$(JOBS) WORKERS=$(WORKERS) HEAP=$(HEAP) \
		MAXSECS=$(QUICK_SECS) QUICK_SECS=$(QUICK_SECS) sh spec/run.sh all

spec-remote:
	@test -n "$(SPEC_HOST)" || { echo "make spec-remote SPEC_HOST=user@host"; exit 2; }
	$(SPEC_REMOTE) start $(SPEC_HOST) $(SPEC_NAMES)

spec-remote-fetch:
	@test -n "$(SPEC_HOST)" || { echo "make spec-remote-fetch SPEC_HOST=user@host"; exit 2; }
	$(SPEC_REMOTE) fetch $(SPEC_HOST)
