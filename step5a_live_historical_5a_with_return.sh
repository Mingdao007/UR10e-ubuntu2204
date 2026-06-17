#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"

POSITION_LOG="$(mktemp -t step5a_position_XXXXXX.log)"
TEST_LOG="$(mktemp -t step5a_historical_5a_XXXXXX.log)"
RETURN_LOG="$(mktemp -t step5a_return_XXXXXX.log)"

cleanup() {
  rm -f "${POSITION_LOG}" "${TEST_LOG}" "${RETURN_LOG}"
}
trap cleanup EXIT

extract_run_dir() {
  local log_path="$1"
  awk -F= '/^run_dir=/{print $2; exit}' "${log_path}"
}

echo "step5a historical combo: position -> test -> return"
echo "phase=position command=step5a_live_historical_5a_position.sh"
if ! "${ROOT}/step5a_live_historical_5a_position.sh" 2>&1 | tee "${POSITION_LOG}"; then
  echo "combo failed at phase=position"
  position_run_dir="$(extract_run_dir "${POSITION_LOG}")"
  if [[ -n "${position_run_dir}" ]]; then
    echo "position_run_dir=${position_run_dir}"
  fi
  exit 2
fi
position_run_dir="$(extract_run_dir "${POSITION_LOG}")"
echo "position_ok=true"
echo "position_run_dir=${position_run_dir}"

echo "phase=test command=step5a_live_historical_5a_test.sh"
if ! "${ROOT}/step5a_live_historical_5a_test.sh" 2>&1 | tee "${TEST_LOG}"; then
  echo "combo failed at phase=test"
  test_run_dir="$(extract_run_dir "${TEST_LOG}")"
  if [[ -n "${test_run_dir}" ]]; then
    echo "test_run_dir=${test_run_dir}"
  fi
  echo "return_not_attempted=true"
  exit 2
fi
test_run_dir="$(extract_run_dir "${TEST_LOG}")"
echo "test_ok=true"
echo "test_run_dir=${test_run_dir}"

if [[ -z "${test_run_dir}" || ! -d "${test_run_dir}" ]]; then
  echo "combo failed before return: could not identify historical test run_dir"
  echo "return_not_attempted=true"
  exit 2
fi

echo "phase=return command=step5a_live_return_to_anchor.sh"
if ! "${ROOT}/step5a_live_return_to_anchor.sh" "${test_run_dir}" 2>&1 | tee "${RETURN_LOG}"; then
  echo "combo failed at phase=return"
  return_run_dir="$(extract_run_dir "${RETURN_LOG}")"
  if [[ -n "${return_run_dir}" ]]; then
    echo "return_run_dir=${return_run_dir}"
  fi
  exit 2
fi
return_run_dir="$(extract_run_dir "${RETURN_LOG}")"
echo "return_ok=true"
echo "return_run_dir=${return_run_dir}"

echo "step5a historical combo complete"
echo "position_run_dir=${position_run_dir}"
echo "test_run_dir=${test_run_dir}"
echo "return_run_dir=${return_run_dir}"
