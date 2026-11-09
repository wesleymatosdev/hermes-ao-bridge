#!/usr/bin/env bash
# Ship gate for hermes-ao-bridge — the definition of "shippable".
#
# Two lanes, both must pass (nonzero exit on any failure or skip):
#   1. python unit suite — stdlib unittest against the in-process fake AO
#      daemon; includes the node-run JS-block contracts (theme layer, board
#      map, …).
#   2. browser E2E       — the REAL frontend rendered in a real headless
#                           Chrome over CDP, asserting the user-visible
#                           contracts (tests/e2e/test_e2e_board.py, one per
#                           shipped v0.8.x bug).
#
# Env knobs:
#   AO_BRIDGE_PYTHON          interpreter to use (default: the Hermes venv
#                             python if present, else python3; needs fastapi +
#                             websockets + uvicorn)
#   AO_BRIDGE_E2E_PLUGIN_ROOT plugin tree under test (default: this checkout)
#                             — the knob RED/GREEN proof runs set to an old
#                             commit's worktree
#   AO_BRIDGE_E2E_ARTIFACT_DIR where proof screenshots go (default: system temp)
#   CHROME_BIN                Chrome/Chromium binary for the E2E lane

set -uo pipefail
cd "$(dirname "$0")/.."

if [ -n "${AO_BRIDGE_PYTHON:-}" ]; then
  PY="$AO_BRIDGE_PYTHON"
elif [ -x "$HOME/.hermes/hermes-agent/.venv/bin/python" ]; then
  PY="$HOME/.hermes/hermes-agent/.venv/bin/python"
else
  PY=python3
fi

rc=0

echo "=== lane 1/2: python unit suite (unittest + node-run JS blocks) ==="
"$PY" -m unittest discover -s tests -t tests || rc=1

echo
echo "=== lane 2/2: browser E2E (real frontend, headless Chrome over CDP) ==="
if ! ls tests/e2e/vendor/react*.min.js >/dev/null 2>&1; then
  echo "FATAL: tests/e2e/vendor is missing the pinned React UMD builds — see tests/e2e/vendor/README.md"
  rc=1
fi
e2e_out="$(AO_BRIDGE_E2E_ARTIFACT_DIR="${AO_BRIDGE_E2E_ARTIFACT_DIR:-}" \
  "$PY" -m unittest tests.e2e.test_e2e_board -v 2>&1)"
echo "$e2e_out"
if [ -z "$e2e_out" ]; then
  echo "SHIP GATE: the E2E lane produced no output — not green."
  rc=1
elif echo "$e2e_out" | grep -q "skipped"; then
  echo "SHIP GATE: the E2E lane SKIPPED (no Chrome?) — a gate that cannot look at the board is not green."
  rc=1
elif ! echo "$e2e_out" | grep -qE "^OK( |$)"; then
  rc=1
fi

echo
if [ "$rc" -eq 0 ]; then
  echo "SHIP GATE: green — suite + E2E passed."
else
  echo "SHIP GATE: RED — do not ship."
fi
exit "$rc"
