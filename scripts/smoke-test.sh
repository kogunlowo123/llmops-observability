#!/usr/bin/env bash
# Exercises the built container image, not the source tree.
#
# The point of this script is to catch the class of defect every other gate in
# the repository is blind to: the image builds, starts, and is still broken
# because something the code needs is not in it. Everything else in the pipeline
# tests the source; this is the only thing that tests the artefact.
#
# `llmops` is a command-line tool with no listener, so there is no health
# endpoint to curl. The equivalent — and the stronger check — is to run the
# project's own gate *inside* the image and assert both that it passes on the
# healthy window and that it still fails, with the right exit code, on the
# broken one.
#
# Rules learned the expensive way, kept because each cost a debugging session:
#
#   1. **Assert against the shipped artefact's own quality gate**, not just its
#      liveness. An image that starts is not an image that works.
#   2. **A binary a tool shells out to must be installed in the image.** The
#      unit tests find it on the developer's PATH; the container is a different
#      machine.
#   3. **Docker Desktop on Windows does not share `mktemp -d` paths.** A bind
#      mount of one silently yields an *empty directory* — no error. Put fixture
#      workspaces under the project directory and convert with `pwd -W`.
#   4. **chmod a bind-mounted directory the image must write to.** The image
#      does not run as root; on Linux a bind mount carries the host's ownership
#      through, so a directory owned by the CI runner's user is one the
#      container's user cannot write to. Docker Desktop on Windows ignores
#      ownership entirely, so this failure appears only in CI.
#   5. **Print the container's stderr when a step fails**, or the assertion
#      names a symptom and hides its cause.
#
# Failures are counted rather than fatal, so one run reports everything that is
# wrong instead of the first thing.
set -euo pipefail

IMAGE="${1:?usage: smoke-test.sh <image[:tag]>}"
failures=0

# Rule 3: a path Docker Desktop will actually share. `pwd -W` yields the Windows
# form under Git Bash and fails everywhere else, where $PWD is already correct.
REPORTS_LOCAL="${PWD}/var/smoke-reports"
mkdir -p "${REPORTS_LOCAL}"
rm -f "${REPORTS_LOCAL}"/* 2>/dev/null || true
# Rule 4: the container writes here as uid 10001.
chmod 0777 "${REPORTS_LOCAL}" 2>/dev/null || true
REPORTS_HOST="$(cd "${REPORTS_LOCAL}" && pwd -W 2>/dev/null || printf %s "${REPORTS_LOCAL}")"

# The examples are baked into the image, so most checks need no mount at all.
run() { docker run --rm --network none "${IMAGE}" "$@"; }

OBJECTIVES="examples/objectives.yaml"
PRICES="examples/prices.yaml"

check() { # check <description> <command...>
  local description="$1"
  shift
  local output
  if output="$("$@" 2>&1)"; then
    echo "  ok    ${description}"
  else
    echo "  FAIL  ${description}"
    printf '%s\n' "${output}" | tail -15 | sed 's/^/          /'   # rule 5
    failures=$((failures + 1))
  fi
}

check_exit() { # check_exit <expected> <description> <command...>
  local expected="$1" description="$2"
  shift 2
  local output status
  set +e
  output="$("$@" 2>&1)"
  status=$?
  set -e
  if [ "${status}" -eq "${expected}" ]; then
    echo "  ok    ${description} (exit ${status})"
  else
    echo "  FAIL  ${description}: expected exit ${expected}, got ${status}"
    printf '%s\n' "${output}" | tail -15 | sed 's/^/          /'   # rule 5
    failures=$((failures + 1))
  fi
}

echo "--- the image is what it claims to be"
check "the default command runs the self-check" \
  run doctor
check "the process does not run as root" \
  docker run --rm --network none --entrypoint sh "${IMAGE}" -c '[ "$(id -u)" != "0" ]'
check "the package is installed, not left as a source path" \
  docker run --rm --network none --entrypoint python "${IMAGE}" -c "import llmops, pathlib; assert 'site-packages' in llmops.__file__, llmops.__file__"

echo
echo "--- rule 1: the shipped image passes its own gate"
# The control. If this is not green then nothing below distinguishes a working
# gate from a broken one.
check_exit 0 "the healthy window holds every objective" \
  run gate --window examples/windows/steady.jsonl.gz \
           --objectives "${OBJECTIVES}" --pricebook "${PRICES}"

echo
echo "--- the gate can still fail, inside the image"
# A gate that has only ever been observed passing is indistinguishable from
# `exit 0`. Exit 2 specifically, not merely non-zero: 3 would mean the tool
# broke, and a job that cannot tell them apart gets retried until it goes green.
check_exit 2 "the outage window breaches an objective" \
  run gate --window examples/windows/outage.jsonl.gz \
           --objectives "${OBJECTIVES}" --pricebook "${PRICES}" \
           --at 2026-09-01T22:40:00+00:00
check_exit 2 "the cost spike breaches the spend objective" \
  run gate --window examples/windows/cost-spike.jsonl.gz \
           --objectives "${OBJECTIVES}" --pricebook "${PRICES}" \
           --at 2026-09-03T12:00:00+00:00

# Exit 3 is a different fact and has to stay distinguishable from exit 2. A
# spend objective without a price book must not be silently skipped.
check_exit 3 "a missing price book is 'could not run', not 'held'" \
  run gate --window examples/windows/steady.jsonl.gz --objectives "${OBJECTIVES}"

echo
echo "--- the corpus and the generator agree inside the artefact"
# Proves the committed windows and the shipped generator are the same build.
# This is the check that catches a corpus regenerated by a different version.
for scenario in steady outage slowdown cost-spike; do
  check_exit 0 "check ${scenario}" \
    run check --scenario "${scenario}" --window "examples/windows/${scenario}.jsonl.gz"
done

echo
echo "--- rule 4: it can write reports to a bind mount"
check "reports are written to a mounted directory" \
  docker run --rm --network none -v "${REPORTS_HOST}:/app/reports" "${IMAGE}" \
    gate --window examples/windows/steady.jsonl.gz \
         --objectives "${OBJECTIVES}" --pricebook "${PRICES}" \
         --json-out reports/slo.json --junit-out reports/slo.xml \
         --markdown-out reports/slo.md
for report in slo.json slo.xml slo.md; do
  if [ -s "${REPORTS_LOCAL}/${report}" ]; then
    echo "  ok    ${report} exists on the host and is not empty"
  else
    echo "  FAIL  ${report} was not written to the bind mount"
    failures=$((failures + 1))
  fi
done

echo
echo "--- nothing reaches the network"
# Every command above already ran with --network none, so a socket would have
# failed them. This asserts the refusal is deliberate rather than incidental.
check_exit 3 "a networked exporter is refused without --allow-network" \
  run synth --scenario steady --out /tmp/w.jsonl --export otlp --endpoint http://127.0.0.1:4318

echo
if [ "${failures}" -eq 0 ]; then
  echo "smoke test passed for ${IMAGE}"
else
  echo "smoke test FAILED for ${IMAGE}: ${failures} assertion(s)"
  exit 1
fi
