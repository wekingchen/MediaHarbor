#!/usr/bin/env bash
# On a GitHub macOS runner, use a genuinely mounted HFS+ disk image to
# exercise CLI source import and cross-filesystem safety without hardware.
set -euo pipefail
[[ "$(uname -s)" == Darwin ]] || { echo 'Only runs on macOS'; exit 2; }
PYTHON="${PYTHON:-python3}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/mediaharbor-ci.XXXXXX")"
MOUNT="$WORK/sd-card"
IMAGE="$WORK/sd-card.sparseimage"
cleanup() {
  hdiutil detach -quiet "$MOUNT" 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT
mkdir -p "$MOUNT" "$WORK/output"
hdiutil create -size 128m -fs HFS+ -volname MHSOURCE -type SPARSE -ov "$IMAGE" >/dev/null
hdiutil attach -quiet -nobrowse -mountpoint "$MOUNT" "$IMAGE"
mkdir -p "$MOUNT/DCIM/100MEDIA"
printf 'photo-12345\n' > "$MOUNT/DCIM/100MEDIA/IMG_0001.JPG"
printf 'video-56789\n' > "$MOUNT/DCIM/100MEDIA/MOV_0001.MP4"
export MEDIA_ARCHIVE_HOME="$WORK/state"
"$PYTHON" media_archive.py configure --target "$WORK/output"
"$PYTHON" media_archive.py scan --source "$MOUNT"
"$PYTHON" media_archive.py scan --source "$MOUNT"
"$PYTHON" - "$WORK" <<'PY'
from pathlib import Path
import sqlite3
import sys
root = Path(sys.argv[1])
pictures = list((root / 'output/图片').glob('*.JPG'))
videos = list((root / 'output/视频').glob('*/*.MP4'))
assert len(pictures) == 1, pictures
assert len(videos) == 1, videos
assert pictures[0].read_bytes() == (root / 'sd-card/DCIM/100MEDIA/IMG_0001.JPG').read_bytes()
assert videos[0].read_bytes() == (root / 'sd-card/DCIM/100MEDIA/MOV_0001.MP4').read_bytes()
con = sqlite3.connect(root / 'state/history.sqlite3')
rows = con.execute('SELECT imported, skipped, failed FROM runs ORDER BY run_id').fetchall()
assert rows == [(2, 0, 0), (0, 2, 0)], rows
print('macOS mounted-volume E2E OK:', rows)
PY
mv "$WORK/output" "$WORK/offline-output"
if "$PYTHON" media_archive.py scan --source "$MOUNT"; then
  echo 'ERROR: missing destination should stop import' >&2
  exit 1
fi
[[ ! -e "$WORK/output" ]] || { echo 'ERROR: recreated offline target'; exit 1; }
echo 'macOS offline-target protection OK'