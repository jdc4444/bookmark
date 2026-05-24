#!/bin/bash
cd /Users/alphaone/Documents/Code/Bookmark
created_run_dir=0
if [ -z "${UPGRADE_RUN_DIR:-}" ]; then
  UPGRADE_RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/bookmark-upgrade.XXXXXX")"
  created_run_dir=1
fi
export UPGRADE_RUN_DIR
mkdir -p "$UPGRADE_RUN_DIR"
cleanup() {
  if [ "$created_run_dir" -eq 1 ]; then
    rm -rf "$UPGRADE_RUN_DIR"
  fi
}
trap cleanup EXIT
ids_file="$UPGRADE_RUN_DIR/ids.txt"
if [ -n "${UPGRADE_IDS_FILE:-}" ]; then
  cp "$UPGRADE_IDS_FILE" "$ids_file"
elif [ -n "${1:-}" ]; then
  cp "$1" "$ids_file"
elif [ ! -f "$ids_file" ]; then
  echo "Set UPGRADE_IDS_FILE or prepopulate $ids_file"
  exit 2
fi
id_count=$(wc -w < "$ids_file" | tr -d ' ')
concurrency="${UPGRADE_CONCURRENCY:-4}"
echo "[$(date '+%H:%M:%S')] start (n=$id_count, concurrency=$concurrency)"
if [ "$id_count" -eq 0 ]; then
  echo "[$(date '+%H:%M:%S')] DONE"
  exit 0
fi
echo "[$(date '+%H:%M:%S')] OCR preflight"
python3 ocr_pipeline.py
rc=$?
if [ $rc -ne 0 ]; then
  echo "[$(date '+%H:%M:%S')] OCR preflight failed (rc=$rc)"
  exit $rc
fi
export UPGRADE_SKIP_WORKER_OCR=1
tr ' ' '\n' < "$ids_file" | xargs -n1 -P "$concurrency" ./upgrade_one.sh
rc=$?
if [ $rc -eq 0 ]; then
  echo "[$(date '+%H:%M:%S')] DONE"
else
  echo "[$(date '+%H:%M:%S')] FAILED (rc=$rc)"
fi
exit $rc
