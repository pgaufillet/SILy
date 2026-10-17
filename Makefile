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

# Host tests: application sources and micropython-lib's unittest, without
# installing anything into the MicroPython tree.
TEST_PATH = .frozen:$(CURDIR)/src:$(MPY_LIB)/python-stdlib/unittest

# Top-level entries of src/ copied to the board's filesystem root.
DEPLOY_ITEMS = $(notdir $(wildcard src/*))

LINT_PATHS = src tests firmware Makefile README.md

.PHONY: help check test lint firmware flash deploy

help:
	@echo "make test                 host tests on the MicroPython unix port"
	@echo "make lint                 ruff (check, format) and codespell"
	@echo "make check                lint + test"
	@echo "make firmware             build the $(BOARD) firmware"
	@echo "make flash PORT=<dev>     flash the $(BOARD) firmware"
	@echo "make deploy PORT=<dev>    copy src/ to the board and reset it"

check: lint test

test:
	MICROPYPATH=$(TEST_PATH) $(MICROPYTHON) tests/run.py tests

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
