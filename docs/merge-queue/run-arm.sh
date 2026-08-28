#!/usr/bin/env bash
# usage: run-arm.sh <LETTER> <INSTRUMENT_PR_or_NONE> <STRATEGY> <WAIT_MINUTES> <BAD_MEMBERS> <MODE>
#
#   LETTER        arm letter (A, B, C, ...); advanced once per cell. The suite judges only THIS arm's
#                 files, so a batch that merged while red cannot poison later cells.
#   INSTRUMENT_PR  a PR number whose merge you want to wait for before this cell runs, or NONE.
#   STRATEGY      ALLGREEN or HEADGREEN -- the merge queue's grouping strategy for this cell.
#   WAIT_MINUTES  min_entries_to_merge_wait_minutes for this cell.
#   BAD_MEMBERS   comma-separated member indices whose presence makes the tree red.
#   MODE always | skip | wait | atomic -- the QUEUE_MODE variable for this cell.
#
# Required environment: SANDBOX_OWNER, SANDBOX_NAME, SANDBOX_REPO, SANDBOX_CHECKOUT, RULESET_ID.

L="$1"
IPR="$2"
STRAT="${3:-ALLGREEN}"
WAIT="${4:-1}"
BAD="${5:-3}"
MODE="${6:-atomic}"

: "${SANDBOX_OWNER:?set SANDBOX_OWNER to the org/user that owns your throwaway repo}"
: "${SANDBOX_NAME:?set SANDBOX_NAME to the throwaway repo name}"
: "${SANDBOX_REPO:?set SANDBOX_REPO to <owner>/<name> of your throwaway repo}"
: "${SANDBOX_CHECKOUT:?set SANDBOX_CHECKOUT to a clone of your throwaway repo}"
: "${RULESET_ID:?set RULESET_ID to the merge-queue ruleset id on that repo}"

SB="$SANDBOX_CHECKOUT"
WORK="${WORK:-$(mktemp -d)}"
R="$SANDBOX_REPO"

trap 'rm -rf "$WORK"' EXIT

if [ "$IPR" != "NONE" ]; then
  echo "STEP waiting for instrument PR $IPR"
  st=""
  for i in $(seq 1 60); do
    st=$(gh pr view "$IPR" --repo "$R" --json state --jq .state 2>/dev/null || echo "")
    [ "$st" = "MERGED" ] && { echo "STEP instrument landed after ${i}0s"; break; }
    sleep 10
  done
  [ "$st" = "MERGED" ] || { echo "STEP FAILED instrument state=$st"; exit 1; }
fi

gh variable set QUEUE_MODE --repo "$R" --body "$MODE" >/dev/null 2>&1 && echo "STEP QUEUE_MODE=$MODE"

cd "$SB"
git fetch -q origin main
git checkout -q --detach origin/main

echo "STEP re-arming check.sh for arg$L (bad members: $BAD)"
git checkout -q -B "exp/arm$L-prep" origin/main

# Pass the checkout path as argv so bash never has to expand a variable inside the heredoc; the old
# `<<'PY'` quoted the delimiter and left `${SANDBOX_CHECKOUT:?...}` literal, which Python then tried
# to use as a directory name. The rig silently wrote into a directory that did not exist.
python3 - "$L" "$BAD" "$SB" <<'PY'
import sys, pathlib
L, BAD, sb = sys.argv[1], sys.argv[2], sys.argv[3]
root = pathlib.Path(sb) / "ci"
root.mkdir(exist_ok=True)

# The MANIFEST is the only thing that changes between arms. The suite below is generic and stays
# byte-identical across the whole grid, so a difference in an arm's outcome can never be a difference
# in the suite's code.
(root / "arm.conf").write_text(f"ARM={L}\nBAD={BAD}\n")

# The suite is deliberately THROWAWAY-scoped: it judges only the CURRENT arm's members, named by the
# manifest. Files left on main by an earlier arm are not mentioned by the new manifest and therefore
# stop being red the moment the next arm lands. That is what keeps a batch that merged while red from
# poisoning every later measurement -- no cleanup commit, no manual repair, and no accumulating error.
(root / "check.sh").write_text(r"""#!/usr/bin/env bash
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
""")
print(f"manifest written: ARM={L} BAD={BAD}; the suite itself is generic")
PY
bash ci/check.sh
echo "STEP prep-suite (pull_request form) exit=$?"

git add -A
git -c user.name="${GIT_AUTHOR_NAME:-rig}" -c user.email="${GIT_AUTHOR_EMAIL:-rig@example.invalid}" commit -q -m "exp(arm $L): re-arm the suite for arg$L"
git push -q -f origin "HEAD:refs/heads/exp/arm$L-prep"

echo "STEP creating four arm-$L members (each carries the check.sh re-arm)"
PRS=""
for i in 1 2 3 4; do
  git checkout -q -B "exp/arg$L-$i" "exp/arm$L-prep"
  mkdir -p exp
  echo "arm $L member $i" > "exp/arg$L-$i.txt"
  git add -A
  git -c user.name="${GIT_AUTHOR_NAME:-rig}" -c user.email="${GIT_AUTHOR_EMAIL:-rig@example.invalid}" commit -q -m "exp(arm $L $i/4): one member"
  git push -q -f origin "HEAD:refs/heads/exp/arg$L-$i"
  url=$(gh pr create --repo "$R" --base main --head "exp/arg$L-$i" \
    --title "exp(arm $L $i/4): $STRAT, bad=$BAD" \
    --body "Strategy $STRAT, mode from QUEUE_MODE, bad members $BAD." 2>&1 | tail -1)
  n=$(printf '%s' "$url" | sed 's|.*/||')
  echo "STEP created PR $n"
  PRS="$PRS $n"
done
echo "STEP prs:$PRS"

echo "STEP arming ruleset: $STRAT, min 4 / wait $WAIT / timeout 30, required 'gate'"
gh api "repos/$R/rulesets/$RULESET_ID" > "$WORK/rs-$L.json"
jq --arg strat "$STRAT" --arg w "$WAIT" \
  '{name, target, enforcement, bypass_actors: (.bypass_actors // []), conditions,
    rules: ([.rules[] | select(.type != "required_status_checks")
      | if .type == "merge_queue" then
          .parameters.min_entries_to_merge = 4
          | .parameters.min_entries_to_merge_wait_minutes = ($w | tonumber)
          | .parameters.check_response_timeout_minutes = 30
          | .parameters.grouping_strategy = $strat
        else . end]
      + [{type: "required_status_checks", parameters: {strict_required_status_checks_policy: false, do_not_enforce_on_create: false, required_status_checks: [{context: "gate"}]}}])}' \
  "$WORK/rs-$L.json" > "$WORK/rs-$L-armed.json"
gh api -X PUT "repos/$R/rulesets/$RULESET_ID" --input "$WORK/rs-$L-armed.json" >/dev/null \
  && echo "STEP armed $STRAT"

echo "STEP waiting for each member's own gate, then enqueueing"
for pr in $PRS; do
  c=""
  for i in $(seq 1 40); do
    c=$(gh pr view "$pr" --repo "$R" --json statusCheckRollup \
      --jq '[.statusCheckRollup[]? | select((.name // .context) == "gate") | (.conclusion // .state)] | first' \
      2>/dev/null || echo "")
    [ "$c" = "SUCCESS" ] || [ "$c" = "FAILURE" ] && break
    sleep 10
  done
  echo "STEP pr $pr own-gate=$c"
  case "$c" in
    SUCCESS) gh pr merge "$pr" --repo "$R" --squash --auto 2>&1 | tail -1 ;;
    "")
      echo "STEP FAILED pr $pr own-gate empty after 400s; this PR will not enter the queue"
      ;;
    PENDING|QUEUED|EXPECTED|REQUESTED)
      echo "STEP FAILED pr $pr own-gate=$c after 400s; this PR will not enter the queue (raise the rig's gate timeout if your checks take longer)"
      ;;
    FAILURE)
      echo "STEP note pr $pr own-gate=FAILURE -- not enqueued; its own red, the cell is short a queue entry"
      ;;
    *) echo "STEP note pr $pr own-gate=$c -- not enqueued" ;;
  esac
done

# Helper: fetch the repo's merge-queue entries as "<pr>:<state>" joined by spaces, or "" on failure.
queue_entries() {
  gh api graphql \
    -F owner="$SANDBOX_OWNER" \
    -F name="$SANDBOX_NAME" \
    -f query='query($owner: String!, $name: String!) {
      repository(owner: $owner, name: $name) {
        mergeQueue { entries(first: 50) { nodes { position state pullRequest { number } } } }
      }
    }' \
    --jq '[.data.repository.mergeQueue.entries.nodes[] | "\(.pullRequest.number):\(.state)"] | join(" ")' \
 2>/dev/null || echo ""
}

# Helper: how many of THIS cell's PRs appear in a "<pr>:<state>" joined string. Counting in this
# cell's terms means a previous cell's leftovers cannot masquerade as the current cell's progress.
count_cell() {
  q="$1"
  n=0
  for pr in $PRS; do
    case " $q " in
      *" $pr:"*) n=$((n + 1)) ;;
    esac
  done
  printf '%s' "$n"
}

qbase="$(queue_entries)"
nbase=$(count_cell "$qbase")
echo "STEP baseline: $nbase of this cell's PRs queued at start of watch"

echo "STEP watching, ceiling 6 minutes"
for i in $(seq 1 36); do
  q="$(queue_entries)"
  echo "t+$((i * 10))s queue=[$q]"
  [ -z "$q" ] && { echo "STEP queue empty"; break; }
  sleep 10
done

qleft="$(queue_entries)"
nleft=$(count_cell "$qleft")

echo "=== CELL mode=$MODE strategy=$STRAT bad=$BAD letter=$L ==="
echo "=== FINAL PRS ==="
for pr in $PRS; do
  gh pr view "$pr" --repo "$R" --json number,state,mergedAt --jq '"\(.number) \(.state) \(.mergedAt // "-")"'
done

echo "=== main holds ==="
gh api "repos/$R/contents/exp" --jq "[.[].name] | map(select(startswith(\"arg$L\")))"

echo "=== SOUNDNESS: judge main by THIS arm's rule, whatever manifest landed ==="
# The trap this replaces: running main's own check.sh is vacuous when the batch did NOT merge, because
# main then still carries the PREVIOUS arm's manifest and the check asks about a letter nobody wrote.
# A green from that is "not about this arm", not "this arm was clean". So the rule is applied here,
# from the arm's own parameters, against whatever main actually holds.
present="$( { ls exp/arg$L-*.txt 2>/dev/null || true; } | tr '\n' ' ' )"
echo "SOUND main holds arm-$L files: [$present]"
rc=0
for b in $(echo "$BAD" | tr ',' ' '); do
  if [ -f "exp/arg$L-$b.txt" ]; then
    echo "SOUND VERDICT=REFUTED -- bad member $b of arm $L reached main"
    rc=1
  fi
done
[ "$rc" -eq 0 ] && echo "SOUND VERDICT=OK -- no bad member of arm $L reached main"

echo "=== LIVENESS: did the queue resolve? ==="
# The baseline-vs-final comparison is the only honest LIVE verdict: a batch that is FALLING has fewer
# of THIS cell's PRs queued at the end than at the start, regardless of what other cells have left.
if [ "$nbase" -eq 0 ] && [ "$nleft" -eq 0 ]; then
  echo "LIVE VERDICT=NOT_QUEUED -- none of this cell's PRs ever entered the queue (check the gate checks above)"
elif [ "$nleft" -eq 0 ]; then
  echo "LIVE VERDICT=RESOLVED -- none of this cell's PRs are queued (started with $nbase, ended with 0)"
elif [ "$nleft" -lt "$nbase" ]; then
  echo "LIVE VERDICT=DRAINING -- $nleft of this cell's PRs still queued at the ceiling (was $nbase at start): [$qleft]"
elif [ "$nleft" -eq "$nbase" ]; then
  echo "LIVE VERDICT=STUCK -- $nleft of this cell's PRs still queued, none moved: [$qleft]"
else
  echo "LIVE VERDICT=DRAINING -- $nleft still queued (was $nbase at start); the queue added entries during the watch"
fi

echo "=== PROGRESSIVE: did the good prefix land? ==="
# Files the rig landed on main, split by whether the arm's manifest considers them good or bad.
# SOUND has already flagged any bad landing; this verdict names the SHAPE of what landed -- full
# prefix, partial prefix, nothing, or REFUTED alongside the good.
good_landed=0
bad_landed=0
for f in $present; do
  member=$(printf '%s' "$f" | sed -n "s|arg${L}-\\([0-9]\\+\\)\\.txt|\\1|p")
  case " $(echo "$BAD" | tr ',' ' ') " in
    *" $member "*) bad_landed=$((bad_landed + 1)) ;;
    *) good_landed=$((good_landed + 1)) ;;
  esac
done
if [ "$bad_landed" -gt 0 ]; then
  echo "PROGRESSIVE VERDICT=REFUTED -- $bad_landed bad member(s) landed alongside the good (SOUND has the details)"
elif [ "$good_landed" -eq 0 ]; then
  echo "PROGRESSIVE VERDICT=NOTHING -- no member of arm $L reached main"
elif [ "$good_landed" -lt 4 ]; then
  echo "PROGRESSIVE VERDICT=PREFIX -- $good_landed of 4 good members reached main, the rest fell together"
else
  echo "PROGRESSIVE VERDICT=FULL -- all 4 members reached main"
fi

echo "=== COST: how many expensive merge_group runs actually completed ==="
runs_json=$(gh run list --repo "$R" --event merge_group --limit 50 \
  --json status,conclusion,headBranch \
  --jq '[.[] | select(.status == "completed")]' 2>/dev/null || echo "[]")
run_count=$(printf '%s' "$runs_json" | jq 'length' 2>/dev/null || echo 0)
echo "=== merge_group runs ==="
printf '%s' "$runs_json" | jq -r '.[] | "\(.status)/\(.conclusion) \(.headBranch | sub("gh-readonly-queue/main/"; ""))"'
echo "CHEAP VERDICT=$run_count expensive merge_group runs executed (completed)"

echo "=== CLEANUP: best-effort -- close OPEN PRs and try to remove from the queue ==="
for pr in $PRS; do
  st=$(gh pr view "$pr" --repo "$R" --json state --jq .state 2>/dev/null || echo "")
  case "$st" in
    OPEN)
      gh pr close "$pr" --repo "$R" --comment "cell $L complete" >/dev/null 2>&1 && echo "closed $pr"
      pid=$(gh pr view "$pr" --repo "$R" --json id --jq .id 2>/dev/null || echo "")
      if [ -n "$pid" ]; then
        gh api graphql -F id="$pid" \
          -f query='mutation($id: ID!) { deleteFromMergeQueue(input: {pullRequestId: $id}) { clientMutationId } }' \
          >/dev/null 2>&1 || true
      fi
      ;;
    MERGED) echo "already merged $pr (nothing to close)" ;;
    CLOSED) echo "already closed $pr" ;;
    *) echo "left $pr state=$st" ;;
  esac
done
echo "NOTE: a cell that left entries mid-merge cannot be cleared by this script. Run cells against a freshly-empty queue at start."
