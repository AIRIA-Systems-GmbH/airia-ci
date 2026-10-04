#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
#
# The coverage gate: the whole suite, Python under coverage.py and bash under
# kcov, as ONE figure, refused under the floor. It runs in the pinned kcov
# image, the same command in CI and on a laptop:
#
#   docker run --rm -v "$PWD:/src" -w /src --entrypoint bash \
#     kcov/kcov@sha256:481289ae32e55e5b733019515acd10948a4f76dfed381765577db909664fc603 tests/coverage.sh
#
# A skipped test fails it too: a skip is not a pass, and here nothing has a
# reason to skip (the image is the GNU userland the Thor runners have).
set -euo pipefail

FLOOR=95
cd "$(dirname "$0")/.."

apt-get -qq update
apt-get -qq install -y --no-install-recommends git jq openssl python3-pip python3-yaml > /dev/null
pip install -q --break-system-packages --root-user-action=ignore coverage==7.16.2

rm -rf build/coverage
mkdir -p build/coverage
export KCOV_OUT="$PWD/build/coverage/kcov"
# Every shipped block on disk before any test runs, so an untested one counts.
python3 -c 'import sys; sys.path.insert(0, "tests"); import steps; steps.materialise()'

python3 -m coverage run -m unittest discover -s tests 2>&1 | tee build/coverage/unittest.log
if grep -q "skipped=" build/coverage/unittest.log; then
  echo "FAIL: tests were skipped in the coverage run" >&2
  exit 1
fi
python3 -m coverage combine -q
python3 -m coverage xml -q -o build/coverage/python.xml
python3 tests/coverage_total.py --floor "$FLOOR"
