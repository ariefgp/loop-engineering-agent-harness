#!/usr/bin/env bash
#
# loop-engineering-dispatcher.sh
#
# Runs every 10 minutes via cron. Scans all registered repos for eligible
# loop-engineering issues. Picks by priority. Invokes the right agent
# (Gibbs/McGee/Jimmy via Claude Code) only when there's real work.
#
# Pre-check gate: zero Claude tokens burned when nothing to do.
# Output is consumed by the cron job's agent prompt.
#
# Exit codes:
#   0 = success (eligible issues found + dispatched, OR no eligible issues)
#   1 = error
set -euo pipefail

# ============================================================
# CONFIG
# ============================================================

# Repos to monitor: "owner/repo|local_path|notes_path"
# notes_path can be empty (no notes repo configured)
REPOS=(
  "Premier-platform/premier-core|/home/ariefgp/repos/premier-core|"
  "Codekarsa/pitipal|/home/ariefgp/repos/pitipal|"
  "Codekarsa/pomofly|/home/ariefgp/repos/pomofly|"
)

# Lock config (used after log() is defined below)
LOCK_FILE="/tmp/loop-engineering-dispatcher.lock"
# PID-liveness staleness: a healthy tick runs 30-50 min (sequential agents).
# File-age-based staleness (10 min) was WRONG — it went "stale" mid-tick and
# spawned duplicate dispatchers. We now check whether the recorded PID is
# actually alive; only a dead PID (crashed dispatcher) is stale.

# Priority order (highest first). Issues with these labels get picked first.
PRIORITY_ORDER="urgent,high,medium,low"

# Claude Code models per role
PM_MODEL="opus"
DEV_MODEL="sonnet"
DEV2_MODEL="sonnet"
QA_MODEL="opus"
QA2_MODEL="opus"

# Resource guard: each agent may start multiple Next.js/Playwright processes
# consuming several GB together. Even two simultaneous agents pushed this
# 8 GB VPS to 6.6 GiB RAM + 1.7 GiB swap. Keep exactly one engineering agent
# live at a time; larger batches run sequentially while the dispatcher lock
# remains held.
MAX_CONCURRENT_AGENTS=1

# ============================================================
# HELPERS
# ============================================================

log() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >&2
}

# ============================================================
# LOCK — prevent overlapping runs
# ============================================================
# Claude Code sessions take 10+ min, but cron fires every 10 min.
# Without a lock, multiple dispatchers + Claude Code sessions pile up
# and overload the VPS (this caused the Jul 26 slowdown).

if [ -f "$LOCK_FILE" ]; then
  LOCK_PID=$(cat "$LOCK_FILE" 2>/dev/null)
  if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
    # Dispatcher PID still alive → genuinely in progress, not stale.
    LOCK_AGE=$(( ($(date +%s) - $(stat -c %Y "$LOCK_FILE" 2>/dev/null || date +%s)) / 60 ))
    RUNNING_AGENT=$(ps -p "$LOCK_PID" -o args= 2>/dev/null | grep -o "\-\-model [a-z0-9.-]*" | head -1 | cut -d' ' -f2 || true)
    echo "# Loop Engineering — scan $(date +%H:%M) WIB"
    echo "  ⏳ Previous run still active (~${LOCK_AGE}min elapsed${RUNNING_AGENT:+, ${RUNNING_AGENT} model})"
    exit 0
  else
    log "Lock is stale (PID ${LOCK_PID:-unknown} dead). Removing and continuing."
    rm -f "$LOCK_FILE"
  fi
fi

echo $$ > "$LOCK_FILE"
TMPFILE=""
cleanup() {
  rm -f "$LOCK_FILE"
  if [ -n "${TMPFILE:-}" ]; then
    rm -f "$TMPFILE" "${TMPFILE}.sorted"
  fi
}
trap cleanup EXIT
log "Lock acquired (PID $$)."

# Get priority weight for an issue (lower = higher priority)
# Returns 99 if no priority label found
priority_weight() {
  local labels_json="$1"
  echo "$labels_json" | python3 -c "
import sys, json
try:
    labels = json.load(sys.stdin)
    label_names = [l.get('name','').lower() for l in labels if isinstance(l, dict)]
    priority_map = {'urgent': 0, 'high': 1, 'medium': 2, 'low': 3}
    for p, w in priority_map.items():
        if p in label_names:
            print(w)
            sys.exit(0)
    print(99)
except:
    print(99)
" 2>/dev/null
}

# ============================================================
# SCAN ALL REPOS FOR ELIGIBLE ISSUES
# ============================================================

log "Scanning ${#REPOS[@]} repos for eligible issues..."

# Collect all eligible issues across all repos and roles
# Format: "repo|path|role|issue_number|issue_title|priority_weight|trigger_label"

ALL_ELIGIBLE=()

for entry in "${REPOS[@]}"; do
  IFS='|' read -r repo path notes <<< "$entry"
  
  # --- PM: check for "to be planned" ---
  pm_issues=$(gh issue list --repo "$repo" --label "to be planned" --state open \
    --json number,title,labels --limit 10 2>/dev/null || echo "[]")
  
  if [ "$pm_issues" != "[]" ]; then
    echo "$pm_issues" | python3 -c "
import sys, json
issues = json.load(sys.stdin)
for i in issues:
    num = i.get('number')
    title = i.get('title','')
    labels = i.get('labels', [])
    label_names = [l.get('name','').lower() for l in labels]
    weight = 99
    for prio, w in [('urgent',0),('high',1),('medium',2),('low',3)]:
        if prio in label_names:
            weight = w
            break
    print(f'${repo}|${path}|pm|{num}|{title[:60]}|{weight}|to be planned')
" 2>/dev/null | while read -r line; do
      ALL_ELIGIBLE+=("$line")
    done
  fi
  
  # --- Dev: check for "todo" and "feedback" ---
  for trigger in "todo" "feedback"; do
    dev_issues=$(gh issue list --repo "$repo" --label "$trigger" --state open \
      --json number,title,labels --limit 10 2>/dev/null || echo "[]")
    
    if [ "$dev_issues" != "[]" ]; then
      echo "$dev_issues" | python3 -c "
import sys, json
issues = json.load(sys.stdin)
for i in issues:
    num = i.get('number')
    title = i.get('title','')
    labels = i.get('labels', [])
    label_names = [l.get('name','').lower() for l in labels]
    weight = 99
    for prio, w in [('urgent',0),('high',1),('medium',2),('low',3)]:
        if prio in label_names:
            weight = w
            break
    print(f'${repo}|${path}|dev|{num}|{title[:60]}|{weight}|${trigger}')
" 2>/dev/null | while read -r line; do
        ALL_ELIGIBLE+=("$line")
      done
    fi
  done
  
  # --- Dev: resume guard — check for stale "in progress" ---
  progress_issues=$(gh issue list --repo "$repo" --label "in progress" --state open \
    --json number,title,labels --limit 5 2>/dev/null || echo "[]")
  if [ "$progress_issues" != "[]" ]; then
    echo "$progress_issues" | python3 -c "
import sys, json
issues = json.load(sys.stdin)
for i in issues:
    num = i.get('number')
    title = i.get('title','')
    print(f'${repo}|${path}|dev|{num}|{title[:60]}|0|in progress (resume)')
" 2>/dev/null | while read -r line; do
      ALL_ELIGIBLE+=("$line")
    done
  fi
  
  # --- QA: check for "qa ready" ---
  qa_issues=$(gh issue list --repo "$repo" --label "qa ready" --state open \
    --json number,title,labels --limit 10 2>/dev/null || echo "[]")
  if [ "$qa_issues" != "[]" ]; then
    echo "$qa_issues" | python3 -c "
import sys, json
issues = json.load(sys.stdin)
for i in issues:
    num = i.get('number')
    title = i.get('title','')
    labels = i.get('labels', [])
    label_names = [l.get('name','').lower() for l in labels]
    weight = 99
    for prio, w in [('urgent',0),('high',1),('medium',2),('low',3)]:
        if prio in label_names:
            weight = w
            break
    print(f'${repo}|${path}|qa|{num}|{title[:60]}|{weight}|qa ready')
" 2>/dev/null | while read -r line; do
      ALL_ELIGIBLE+=("$line")
    done
  fi
  
  # --- QA: resume guard — check for stale "qa in progress" ---
  qa_prog_issues=$(gh issue list --repo "$repo" --label "qa in progress" --state open \
    --json number,title,labels --limit 5 2>/dev/null || echo "[]")
  if [ "$qa_prog_issues" != "[]" ]; then
    echo "$qa_prog_issues" | python3 -c "
import sys, json
issues = json.load(sys.stdin)
for i in issues:
    num = i.get('number')
    title = i.get('title','')
    print(f'${repo}|${path}|qa|{num}|{title[:60]}|0|qa in progress (resume)')
" 2>/dev/null | while read -r line; do
      ALL_ELIGIBLE+=("$line")
    done
  fi
done

# ============================================================
# SORT BY PRIORITY AND DISPATCH
# ============================================================

# Write eligible issues to a temp file for sorting
TMPFILE=$(mktemp)

# Re-collect from the arrays (they were lost in subshells)
for entry in "${REPOS[@]}"; do
  IFS='|' read -r repo path notes <<< "$entry"
  
  for trigger_spec in \
    "pm:to be planned" \
    "dev:todo" \
    "dev:feedback" \
    "dev:in progress (resume)" \
    "qa:qa ready" \
    "qa:qa in progress (resume)"; do
    
    role="${trigger_spec%%:*}"
    label="${trigger_spec#*:}"
    
    # Map display label to actual gh search label
    case "$label" in
      "in progress (resume)") gh_label="in progress" ;;
      "qa in progress (resume)") gh_label="qa in progress" ;;
      *) gh_label="$label" ;;
    esac
    
    issues=$(gh issue list --repo "$repo" --label "$gh_label" --state open \
      --json number,title,labels --limit 10 2>/dev/null || echo "[]")
    
    if [ "$issues" != "[]" ]; then
      echo "$issues" | python3 -c "
import sys, json
issues = json.load(sys.stdin)
for i in issues:
    num = i.get('number')
    title = i.get('title','')[:60]
    labels = i.get('labels', [])
    label_names = [l.get('name','').lower() for l in labels]
    weight = 99
    for prio, w in [('urgent',0),('high',1),('medium',2),('low',3)]:
        if prio in label_names:
            weight = w
            break
    # resume items get weight 0 (highest)
    if 'resume' in '''$label''':
        weight = 0
    print(f'{weight}\t${repo}\t${path}\t${role}\t{num}\t{title}\t${label}')
" 2>/dev/null >> "$TMPFILE" || true
    fi
  done
done

# Sort by priority weight (ascending = highest priority first)
sort -t$'\t' -k1 -n "$TMPFILE" > "${TMPFILE}.sorted"

ELIGIBLE_COUNT=$(wc -l < "${TMPFILE}.sorted" | tr -d ' ')

if [ "$ELIGIBLE_COUNT" -eq 0 ]; then
  log "No eligible issues across any repo. Exiting (zero tokens burned)."
  # Silent exit — cron job stays quiet when nothing to do
  exit 0
fi

log "Found $ELIGIBLE_COUNT eligible issue(s) across repos."
SCAN_TIME=$(date +%H:%M)
echo "# Loop Engineering — scan ${SCAN_TIME} WIB"

# Dispatch up to: 1 PM, 2 Dev (McGee + Torres), 2 QA (Jimmy + Ducky) per tick.
# Jobs run sequentially under MAX_CONCURRENT_AGENTS=1. This preserves the full
# roster without allowing overlapping Next.js/Playwright workloads to exhaust RAM.

declare -A DISPATCHED=()  # role -> count
declare -i DEV_COUNT=0
declare -i QA_COUNT=0

# --- Pass 1: collect dispatch jobs (agent|model|file|role|repo|path|num) ---
JOBS=()
while IFS=$'\t' read -r weight repo path role num title label; do
  case "$role" in
    pm) [ "${DISPATCHED[pm]:-0}" -ge 1 ] && continue ;;
    dev) [ "$DEV_COUNT" -ge 2 ] && continue ;;
    qa) [ "$QA_COUNT" -ge 2 ] && continue ;;
  esac

  case "$role" in
    pm) AGENT="Gibbs"; MODEL="$PM_MODEL"; AGENT_FILE="agent-pm.md" ;;
    dev)
      if [ "$DEV_COUNT" -eq 0 ]; then AGENT="McGee"; MODEL="$DEV_MODEL"; AGENT_FILE="agent-dev.md";
      else AGENT="Torres"; MODEL="$DEV2_MODEL"; AGENT_FILE="agent-dev-torres.md"; fi
      DEV_COUNT=$((DEV_COUNT + 1)) ;;
    qa)
      if [ "$QA_COUNT" -eq 0 ]; then AGENT="Jimmy"; MODEL="$QA_MODEL"; AGENT_FILE="agent-qa.md";
      else AGENT="Ducky"; MODEL="$QA2_MODEL"; AGENT_FILE="agent-qa-ducky.md"; fi
      QA_COUNT=$((QA_COUNT + 1)) ;;
  esac

  JOBS+=("$AGENT|$MODEL|$AGENT_FILE|$role|$repo|$path|$num")
done < "${TMPFILE}.sorted"

# --- Pass 2: launch jobs in resource-safe waves ---
declare -a JOB_META=()
declare -i ACTIVE_JOBS=0
for job in "${JOBS[@]}"; do
  IFS='|' read -r AGENT MODEL AGENT_FILE role repo path num <<< "$job"
  CLAUDE_CMD="claude -p --model $MODEL --permission-mode auto --output-format json --add-dir $path -- \"Read .agents/WORKFLOW.md and .agents/$AGENT_FILE, then execute the recurring $role task for $repo. The highest-priority eligible issue is #$num. You are acting as $AGENT.\""
  log "Running: $AGENT ($role) → $repo #$num (wave concurrency ${MAX_CONCURRENT_AGENTS})"
  outfile=$(mktemp)
  exitfile=$(mktemp)
  ( set +e; eval "$CLAUDE_CMD" > "$outfile" 2>&1; echo $? > "$exitfile" ) &
  JOB_META+=("$AGENT|$num|$outfile|$exitfile")
  ACTIVE_JOBS=$((ACTIVE_JOBS + 1))

  if [ "$ACTIVE_JOBS" -ge "$MAX_CONCURRENT_AGENTS" ]; then
    log "Resource cap reached (${MAX_CONCURRENT_AGENTS}); waiting for this wave to finish."
    wait || true
    ACTIVE_JOBS=0
  fi
done

# Wait for a final partial wave, if any.
wait || true

for meta in "${JOB_META[@]}"; do
  IFS='|' read -r AGENT num outfile exitfile <<< "$meta"
  CLAUDE_OUTPUT=$(cat "$outfile" 2>/dev/null || true)
  CLAUDE_EXIT=$(cat "$exitfile" 2>/dev/null || echo 1)

  CLAUDE_RESULT=$(echo "$CLAUDE_OUTPUT" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    result = data.get('result', '')
    lines = result.strip().split('\n')[:3]
    print('\n'.join(lines))
except:
    print('(unable to parse result)')
" 2>/dev/null)

  COST=$(echo "$CLAUDE_OUTPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'\${d.get(\"total_cost_usd\",0):.2f}')" 2>/dev/null || echo '?')
  TIME=$(echo "$CLAUDE_OUTPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'{d.get(\"duration_ms\",0)//1000}s')" 2>/dev/null || echo '?')
  ENGINE=$([ "$CLAUDE_EXIT" -eq 0 ] && echo 'cc' || echo 'fb')

  echo "  $AGENT → #$num: $CLAUDE_RESULT"
  echo "  💰$COST  |  ⏱$TIME  |  [$ENGINE]"
  echo ""

  rm -f "$outfile" "$exitfile"
done
