#!/bin/bash
id="$1"
cd /Users/alphaone/Documents/Code/Bookmark
created_run_dir=0
if [ -z "${UPGRADE_RUN_DIR:-}" ]; then
  UPGRADE_RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/bookmark-upgrade-one.XXXXXX")"
  export UPGRADE_RUN_DIR
  created_run_dir=1
  printf '%s\n' "$id" > "$UPGRADE_RUN_DIR/ids.txt"
fi
cleanup() {
  if [ "$created_run_dir" -eq 1 ]; then
    rm -rf "$UPGRADE_RUN_DIR"
  fi
}
trap cleanup EXIT
if [ -z "$id" ] && [ -f "$UPGRADE_RUN_DIR/ids.txt" ]; then
  id="$(head -n 1 "$UPGRADE_RUN_DIR/ids.txt")"
fi
out_file="$UPGRADE_RUN_DIR/upgrade_${id}.out"
python3 -c "
import sys; sys.path.insert(0, '.')
import subprocess, json, os
import article_generator as ag
import ocr_pipeline as ocrp

bm = next((b for b in json.load(open('data/x-bookmarks.json'))['bookmarks'] if b['id']=='$id'), None)
if not bm:
    sys.exit(2)

# Belt-and-suspenders: ensure every image (own, quoted, threaded) is OCR'd
# before article-gen. The pipeline is a no-op when nothing's missing (~1s),
# so this is a small price to pay for guaranteed OCR context in every brief.
def has_unocrd_image(bm):
    return bool(ocrp.collect_targets([bm]))

if has_unocrd_image(bm) and not os.environ.get('UPGRADE_SKIP_WORKER_OCR'):
    ocr = subprocess.run(['python3','ocr_pipeline.py'], cwd='.', timeout=900,
                         capture_output=True, text=True)
    if ocr.returncode != 0:
        detail = (ocr.stderr or ocr.stdout or '').strip().splitlines()
        raise RuntimeError('ocr_pipeline failed: ' + (detail[-1] if detail else f'rc={ocr.returncode}'))
    # Re-read bookmark with fresh OCR
    bm = next(b for b in json.load(open('data/x-bookmarks.json'))['bookmarks'] if b['id']=='$id')

article = ag.generate_article(bm, prefer='claude', use_cache=False, write=False)
ag.write_article('$id', article, archive=True)
" > "$out_file" 2>&1
rc=$?
if [ $rc -eq 0 ]; then
  echo "[$(date '+%H:%M:%S')] ok $id"
else
  err=$(tail -1 "$out_file")
  echo "[$(date '+%H:%M:%S')] fail $id ($err)"
fi
exit $rc
