# Thin alias layer over scripts/.
#
# scripts/*.sh are the canonical entrypoints — they are what the README documents
# and what CI calls, because `make` is not present on every dev machine here
# (Windows + Git Bash). This file exists so `make test` works for contributors who
# expect it. Never put logic here; put it in the script.

SHELL := /usr/bin/env bash

.PHONY: help validate lint up down smoke verify gen logs test measure

help:
	@echo "Targets (each just calls the matching scripts/<name>.sh):"
	@echo "  validate  - validate Collector configs against the pinned binary"
	@echo "  lint      - yaml / python / shell checks"
	@echo "  up        - bring the stack up            [Phase 1]"
	@echo "  down      - tear the stack down           [Phase 1]"
	@echo "  smoke     - backend readiness checks      [Phase 1]"
	@echo "  verify    - END-TO-END PIPELINE GATE: prove all 3 signals land"
	@echo "  gen       - drive synthetic telemetry with telemetrygen"
	@echo "  logs      - tail stack logs (make logs s=loki)"
	@echo "  test      - unit + integration tests      [Phase 5]"
	@echo "  measure   - before/after reduction report [Phase 3]"

validate: ; @bash scripts/validate.sh
lint:     ; @bash scripts/lint.sh
up:       ; @bash scripts/up.sh
down:     ; @bash scripts/down.sh
smoke:    ; @bash scripts/smoke.sh
verify:   ; @bash scripts/verify-pipeline.sh
gen:      ; @bash scripts/telemetrygen.sh
logs:     ; @bash scripts/logs.sh $(s)
test:     ; @bash scripts/test.sh
measure:  ; @bash scripts/measure.sh
