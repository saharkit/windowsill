#!/usr/bin/env bash
# The stand-in for an EXPENSIVE suite. Generic: the arm and its bad members come from ci/arm.conf.
#
# THE PR CHECK AND THE QUEUE CHECK ARE DIFFERENT ON PURPOSE, and it mirrors what a real repository
# already does — an affected-set split on a pull request, the FULL suite in the merge group. It is
# also what makes the whole position axis measurable: a pull request that is red ON ITS OWN never
# enters the merge queue, so without this asymmetry "the breakage is in the FIRST member" could not
# be measured at all. With it, every member enters green and the queue is where the truth comes out.
#
#   pull_request : always passes — the weaker, cheaper check.
#   merge_group  : the real rule — red if any BAD member of the CURRENT arm is in this tree.
#
# The arm letter advances every cell, which is what makes the rig self-healing: a batch that merged
# while red leaves files the NEXT arm's manifest does not name, so the base branch returns to green by
# itself rather than by a cleanup commit. Without that, one bad cell poisons every later measurement.
set -euo pipefail
# shellcheck disable=SC1091
. ci/arm.conf
if [ "${GITHUB_EVENT_NAME:-}" != "merge_group" ]; then
  echo "check.sh: pull_request (or local) -- the reduced check, which passes by construction"
  exit 0
fi
# The expensive suite takes TIME. Without it every job finishes in the same second and the modes are
# indistinguishable in wall-clock: a skip that saves nothing looks like a skip that saves a suite.
sleep 5
# `set -e` + `pipefail` + a glob that matches NOTHING is a trap: `ls` exits 2, pipefail propagates it,
# and the script dies before deciding anything. The zero-file case is NORMAL on a fresh base branch.
present="$( { ls exp/arg${ARM}-*.txt 2>/dev/null || true; } | tr '\n' ' ' )"
echo "check.sh: arm=${ARM} bad=${BAD} tree holds [${present}]"
rc=0
IFS=','
for b in ${BAD}; do
  if [ -f "exp/arg${ARM}-${b}.txt" ]; then
    echo "check.sh: FAIL -- bad member ${b} of arm ${ARM} is in this candidate's tree"
    rc=1
  fi
done
unset IFS
[ "$rc" -eq 0 ] && echo "check.sh: pass"
exit "$rc"
