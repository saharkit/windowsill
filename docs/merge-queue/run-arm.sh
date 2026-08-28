#!/usr/bin/env bash
# usage: run-arm.sh <LETTER> <INSTRUMENT_PR_or_NONE> <STRATEGY>
export PATH="$HOME/.local/bin:$PATH"
L="$1"; IPR="$2"; STRAT="${3:-ALLGREEN}"; WAIT="${4:-1}"; BAD="${5:-3}"; MODE="${6:-atomic}"
SB=${SANDBOX_CHECKOUT:?set SANDBOX_CHECKOUT to a clone of your throwaway repo}
WORK="${WORK:-$(mktemp -d)}"
R=${SANDBOX_REPO:?set SANDBOX_REPO to <owner>/<name> of your throwaway repo}

if [ "$IPR" != "NONE" ]; then
  echo "STEP waiting for instrument PR $IPR"
  for i in $(seq 1 60); do
    st=$(gh pr view "$IPR" --repo $R --json state --jq .state 2>/dev/null)
    [ "$st" = "MERGED" ] && { echo "STEP instrument landed after ${i}0s"; break; }
    sleep 10
  done
  [ "$st" = "MERGED" ] || { echo "STEP FAILED instrument state=$st"; exit 1; }
fi

gh variable set QUEUE_MODE --repo $R --body "$MODE" >/dev/null 2>&1 && echo "STEP QUEUE_MODE=$MODE"
cd $SB; git fetch -q origin main; git checkout -q --detach origin/main
echo "STEP re-arming check.sh for arg$L (bad members: $BAD)"
git checkout -q -B exp/arm$L-prep origin/main
python3 - "$L" "$BAD" <<'PY'
import sys, pathlib
L, BAD = sys.argv[1], sys.argv[2]
root = pathlib.Path("${SANDBOX_CHECKOUT:?set SANDBOX_CHECKOUT to a clone of your throwaway repo}/ci")
root.mkdir(exist_ok=True)

# The MANIFEST is the only thing that changes between arms. The suite below is generic and stays
# byte-identical across the whole grid, so a difference in an arm's outcome can never be a difference
# in the suite's code.
(root / "arm.conf").write_text(f"ARM={L}\nBAD={BAD}\n")

# The suite is deliberately THROWAWAY-scoped: it judges only the CURRENT arm's members, named by the
# manifest. Files left on main by an earlier arm are not mentioned by the new manifest and therefore
# stop being red the moment the next arm lands. That is what keeps a batch that merged while red from
# poisoning every later measurement -- no cleanup commit, no manual repair, and no accumulating error.
(root / "check.sh").write_text("""#!/usr/bin/env bash
# The stand-in for an EXPENSIVE suite. Generic: the arm and its bad members come from ci/arm.conf.
#
# THE PR CHECK AND THE QUEUE CHECK ARE DIFFERENT ON PURPOSE, and it mirrors what the real repository
# already does -- an affected-set split on a pull request, the FULL suite in the merge group. It is
# also what makes the whole position axis measurable: a pull request that is red ON ITS OWN never
# enters the merge queue, so without this asymmetry "the breakage is in the FIRST member" could not
# be measured at all. With it, every member enters green and the queue is where the truth comes out.
#
#   pull_request : always passes -- the weaker, cheaper check.
#   merge_group  : the real rule -- red if any BAD member of the CURRENT arm is in this tree.
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
present="$( { ls exp/arg${ARM}-*.txt 2>/dev/null || true; } | tr '\\n' ' ' )"
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
""")
print(f"manifest written: ARM={L} BAD={BAD}; the suite itself is generic")
PY
bash ci/check.sh; echo "STEP prep-suite (pull_request form) exit=$?"
# COMMIT the re-arm on the prep branch. Leaving it as a working-tree change is a trap that
# invalidated arm G: the FIRST member's `git add -A` swept it up, and `git checkout -B` for members
# 2..4 reset to prep and lost it -- so three of the four candidates carried the PREVIOUS arm's
# check.sh and the head's suite counted a letter nobody was writing.
git add -A
git -c user.name="${GIT_AUTHOR_NAME:-rig}" -c user.email="${GIT_AUTHOR_EMAIL:-rig@example.invalid}" commit -q -m "exp(arm $L): re-arm the suite for arg$L"
git push -q -f origin HEAD:refs/heads/exp/arm$L-prep

echo "STEP creating four arm-$L members (each carries the check.sh re-arm)"
PRS=""
for i in 1 2 3 4; do
  git checkout -q -B exp/arg$L-$i exp/arm$L-prep
  mkdir -p exp; echo "arm $L member $i" > exp/arg$L-$i.txt
  git add -A
  git -c user.name="${GIT_AUTHOR_NAME:-rig}" -c user.email="${GIT_AUTHOR_EMAIL:-rig@example.invalid}" commit -q -m "exp(arm $L $i/4): one member"
  git push -q -f origin HEAD:refs/heads/exp/arg$L-$i
  n=$(gh pr create --repo $R --base main --head exp/arg$L-$i \
    --title "exp(arm $L $i/4): $STRAT, bad=$BAD" \
    --body "Strategy $STRAT, mode from QUEUE_MODE, bad members $BAD." 2>&1 | tail -1 | sed 's|.*/||')
  echo "STEP created PR $n"
  PRS="$PRS $n"
done
echo "STEP prs:$PRS"

echo "STEP arming ruleset: $STRAT, min 4 / wait $WAIT / timeout 30, required 'gate'"
gh api repos/$R/rulesets/${RULESET_ID:?set RULESET_ID to the merge-queue ruleset id on that repo} > $WORK/rs-$L.json
jq --arg strat "$STRAT" --arg w "$WAIT" '{name,target,enforcement,bypass_actors:(.bypass_actors//[]),conditions,
  rules: ([.rules[]|select(.type!="required_status_checks")
    |if .type=="merge_queue" then .parameters.min_entries_to_merge=4
       |.parameters.min_entries_to_merge_wait_minutes=($w|tonumber)
       |.parameters.check_response_timeout_minutes=30
       |.parameters.grouping_strategy=$strat else . end]
    + [{type:"required_status_checks",parameters:{strict_required_status_checks_policy:false,do_not_enforce_on_create:false,required_status_checks:[{context:"gate"}]}}])}' $WORK/rs-$L.json > $WORK/rs-$L-armed.json
gh api -X PUT repos/$R/rulesets/${RULESET_ID:?set RULESET_ID to the merge-queue ruleset id on that repo} --input $WORK/rs-$L-armed.json >/dev/null && echo "STEP armed $STRAT"

echo "STEP waiting for each member's own gate, then enqueueing"
for pr in $PRS; do
  for i in $(seq 1 40); do
    c=$(gh pr view $pr --repo $R --json statusCheckRollup --jq '[.statusCheckRollup[]?|select((.name//.context)=="gate")|(.conclusion//.state)]|first' 2>/dev/null)
    [ "$c" = "SUCCESS" ] || [ "$c" = "FAILURE" ] && break
    sleep 10
  done
  echo "STEP pr $pr own-gate=$c"
  [ "$c" = "SUCCESS" ] && gh pr merge $pr --repo $R --squash --auto 2>&1 | tail -1
done

echo "STEP watching, ceiling 6 minutes"
for i in $(seq 1 36); do
  q=$(gh api graphql -f query='query{repository(owner:"${SANDBOX_OWNER:?set SANDBOX_OWNER}",name:"${SANDBOX_NAME:?set SANDBOX_NAME}"){mergeQueue{entries(first:20){nodes{position state pullRequest{number}}}}}}' --jq '[.data.repository.mergeQueue.entries.nodes[]|"\(.pullRequest.number):\(.state)"]|join(" ")' 2>/dev/null)
  echo "t+$((i*10))s queue=[$q]"
  [ -z "$q" ] && { echo "STEP queue empty"; break; }
  sleep 10
done
# A cell that did not resolve inside the ceiling must clean up after itself, or its stuck entries
# become the next cell's starting condition and every later verdict is contaminated.
qstuck="$(gh api graphql -f query='query{repository(owner:"${SANDBOX_OWNER:?set SANDBOX_OWNER}",name:"${SANDBOX_NAME:?set SANDBOX_NAME}"){mergeQueue{entries(first:20){nodes{pullRequest{number}}}}}}' --jq '[.data.repository.mergeQueue.entries.nodes[].pullRequest.number]|join(" ")' 2>/dev/null)"
if [ -n "$qstuck" ]; then
  echo "STEP cell did not resolve; dequeuing [$qstuck] so the next cell starts clean"
fi
echo "=== CELL mode=$MODE strategy=$STRAT bad=$BAD letter=$L ==="
echo "=== FINAL PRS ==="
for pr in $PRS; do gh pr view $pr --repo $R --json number,state,mergedAt --jq '"\(.number) \(.state) \(.mergedAt // "-")"'; done
echo "=== main holds ==="
gh api repos/$R/contents/exp --jq "[.[].name]|map(select(startswith(\"arg$L\")))"
echo "=== SOUNDNESS: judge main by THIS arm's rule, whatever manifest landed ==="
# The trap this replaces: running main's own check.sh is vacuous when the batch did NOT merge, because
# main then still carries the PREVIOUS arm's manifest and the check asks about a letter nobody wrote.
# A green from that is "not about this arm", not "this arm was clean". So the rule is applied here,
# from the arm's own parameters, against whatever main actually holds.
cd $SB; git fetch -q origin main; git checkout -q --detach origin/main
present="$( { ls exp/arg$L-*.txt 2>/dev/null || true; } | tr '\n' ' ' )"
echo "SOUND main holds arm-$L files: [${present}]"
rc=0
for b in $(echo "$BAD" | tr ',' ' '); do
  if [ -f "exp/arg$L-$b.txt" ]; then
    echo "SOUND VERDICT=REFUTED — bad member $b of arm $L reached main"
    rc=1
  fi
done
[ "$rc" -eq 0 ] && echo "SOUND VERDICT=OK — no bad member of arm $L reached main"
echo "=== LIVENESS: did the queue resolve? ==="
qleft="$(gh api graphql -f query='query{repository(owner:"${SANDBOX_OWNER:?set SANDBOX_OWNER}",name:"${SANDBOX_NAME:?set SANDBOX_NAME}"){mergeQueue{entries(first:20){nodes{position state pullRequest{number}}}}}}' --jq '[.data.repository.mergeQueue.entries.nodes[]|"\(.pullRequest.number):\(.state)"]|join(" ")' 2>/dev/null)"
# STUCK is two different things and conflating them cost a wrong reading on cell K. A batch that is
# FALLING (entries leaving as they turn red) is progressing, just slower than the ceiling; a batch
# where every entry sits unchanged is the deadlock. The distinguishing fact is whether the entry set
# SHRANK during the watch, so it is measured rather than judged.
nleft=$(printf '%s' "$qleft" | tr ' ' '\n' | grep -c ':' || true)
if [ -z "$qleft" ]; then
  echo "LIVE VERDICT=RESOLVED — queue empty"
elif [ "$nleft" -lt 4 ]; then
  echo "LIVE VERDICT=DRAINING — $nleft of 4 left at the ceiling, the rest already departed: [$qleft]"
else
  echo "LIVE VERDICT=STUCK — all 4 still queued, nothing moved: [$qleft]"
fi
echo "=== COST: expensive suite runs actually executed in this cell ==="
echo "=== merge_group runs ==="
gh run list --repo $R --event merge_group --limit 12 --json status,conclusion,headBranch --jq '.[]|"\(.status)/\(.conclusion) \(.headBranch|sub("gh-readonly-queue/main/";""))"'

echo "=== CLEANUP: close anything of this arm still open ==="
for pr in $PRS; do
  st=$(gh pr view $pr --repo $R --json state --jq .state 2>/dev/null)
  [ "$st" = "OPEN" ] && gh pr close $pr --repo $R --comment "cell $L complete" >/dev/null 2>&1 && echo "closed $pr"
done
