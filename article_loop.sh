#!/bin/bash
cd /Users/alphaone/Documents/Code/Bookmark
LOG=/Users/alphaone/Documents/Code/Bookmark/data/article_loop.log
cleanup_loop_run() {
  if [ -n "${loop_run_dir:-}" ]; then
    rm -rf "$loop_run_dir"
  fi
}
trap cleanup_loop_run EXIT

while true; do
  ts=$(date '+%Y-%m-%d %H:%M:%S')
  loop_run_dir="$(mktemp -d "${TMPDIR:-/tmp}/bookmark-upgrade-loop.XXXXXX")"
  ids_file="$loop_run_dir/ids.txt"
  selector_err="$loop_run_dir/selector.err"

  # Find past-7-day bookmarks that don't have a claude-sonnet-subagent article
  python3 - > "$ids_file" 2> "$selector_err" <<'PY'
import json, os
from datetime import datetime, timezone, timedelta
bms = json.load(open('data/x-bookmarks.json'))['bookmarks']
arts = {f[len('article_'):-len('.json')]: f for f in os.listdir('data/articles') if f.startswith('article_') and f.endswith('.json')}
cutoff = datetime.now(timezone.utc) - timedelta(days=7)
def parse(s):
    try: return datetime.fromisoformat((s or '').replace('Z','+00:00'))
    except: return None
todo = []
for b in bms:
    ts = parse(b.get('created_at'))
    if not ts or ts < cutoff: continue
    bid = b['id']
    if bid not in arts:
        todo.append(bid); continue
    a = json.load(open(f'data/articles/{arts[bid]}'))
    if a.get('backend') != 'claude-sonnet-subagent':
        todo.append(bid)
print(' '.join(todo))
PY
  selector_rc=$?
  if [ "$selector_rc" -ne 0 ]; then
    echo "[$ts] bookmark id selector failed rc=$selector_rc; will retry" >> "$LOG"
    if [ -s "$selector_err" ]; then
      sed "s/^/[$ts] selector stderr: /" "$selector_err" >> "$LOG"
    fi
    rm -rf "$loop_run_dir"
    sleep 300
    continue
  fi

  n=$(wc -w < "$ids_file" | tr -d ' ')
  if [ "$n" -eq 0 ]; then
    echo "[$ts] all past-7-day bookmarks have subagent articles — exiting" >> "$LOG"
    rm -rf "$loop_run_dir"
    exit 0
  fi
  echo "[$ts] tick: $n bookmarks need articles" >> "$LOG"

  # Avoid concurrent runs — if a previous pipeline is still alive, skip.
  if pgrep -f "($PWD/upgrade_all.sh|\\./upgrade_all.sh)" > /dev/null; then
    echo "[$ts] previous run still in progress, sleeping" >> "$LOG"
  else
    > data/upgrade.log
    UPGRADE_IDS_FILE="$ids_file" ./upgrade_all.sh >> data/upgrade.log 2>&1
    rc=$?
    ok=$(grep -c "^\[.*\] ok " data/upgrade.log || true)
    fail=$(grep -c "^\[.*\] fail " data/upgrade.log || true)
    if [ "$rc" -eq 0 ]; then
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] pipeline run done: ok=$ok fail=$fail" >> "$LOG"
    else
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] pipeline run failed rc=$rc: ok=$ok fail=$fail" >> "$LOG"
    fi
  fi

  rm -rf "$loop_run_dir"
  sleep 300
done
