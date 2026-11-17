#!/usr/bin/env bash
# Seeded-defect acceptance check for the Quint model: inject a known bug, require Apalache to find it.
#
# The marker line in the misfire action ("a misfire never counts as accepted") is replaced by
# "accepted' = accepted + 1", which breaks accepted_frames_only. The model itself is never modified.
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
quint=${QUINT:-quint}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cp "$here/capture.qnt" "$here/invariants.qnt" "$tmp/"

python3 - "$tmp/capture.qnt" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
s = p.read_text()
marker = "accepted' = accepted,     // SEEDED-DEFECT marker: a misfire never counts as accepted"
if marker not in s:
    sys.exit("seeded-defect marker missing from capture.qnt")
p.write_text(s.replace(marker, "accepted' = accepted + 1,"))
PY

server=()
[ -n "${APALACHE_SERVER:-}" ] && server=(--server-endpoint "$APALACHE_SERVER")
set +e
"$quint" verify "$tmp/capture.qnt" --main=capture --init=init --step=step --max-steps=12 \
  "${server[@]}" --invariant=accepted_frames_only \
  --out-itf="$tmp/caught.itf.json" > "$tmp/verify.log" 2>&1
status=$?
set -e
if [ "$status" -eq 0 ]; then
  echo "seeded defect was NOT caught" >&2
  exit 1
fi
echo "seeded defect caught (exit $status); trace: $tmp/caught.itf.json (not written into the repo)"
tail -3 "$tmp/verify.log"
