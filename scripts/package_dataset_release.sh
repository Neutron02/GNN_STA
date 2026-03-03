#!/usr/bin/env bash
set -euo pipefail

# Create a lossless dataset archive for external sharing (e.g., GitHub Releases),
# including logs and manifests alongside raw/processed data.
#
# Usage:
#   scripts/package_dataset_release.sh [output_dir] [tag] [chunk_size]
#
# Defaults:
#   output_dir  = "$HOME/dataset_exports"
#   tag         = current local timestamp (YYYYMMDD_HHMMSS)
#   chunk_size  = 1900m  (safe for GitHub Release upload limits)

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${1:-$HOME/dataset_exports}"
TAG="${2:-$(date +%Y%m%d_%H%M%S)}"
CHUNK_SIZE="${3:-1900m}"

mkdir -p "$OUT_DIR"

INCLUDE_PATHS=(
  "data/raw_curated"
  "data/processed"
  "data/manifests"
  "logs/pipeline"
)

for rel in "${INCLUDE_PATHS[@]}"; do
  if [[ ! -e "$ROOT_DIR/$rel" ]]; then
    echo "Missing required path: $ROOT_DIR/$rel" >&2
    exit 1
  fi
done

BASE="gnn_sta_dataset_${TAG}"
ARCHIVE="$OUT_DIR/${BASE}.tar.gz"

echo "Root:      $ROOT_DIR"
echo "Output:    $OUT_DIR"
echo "Tag:       $TAG"
echo "Chunk:     $CHUNK_SIZE"
echo "Archive:   $ARCHIVE"
echo "Includes:"
for rel in "${INCLUDE_PATHS[@]}"; do
  echo "  - $rel"
done

(
  cd "$ROOT_DIR"
  tar -cf - "${INCLUDE_PATHS[@]}" | gzip -1 > "$ARCHIVE"
)

split -b "$CHUNK_SIZE" -d -a 3 "$ARCHIVE" "$OUT_DIR/${BASE}.tar.gz.part-"

(
  cd "$OUT_DIR"
  sha256sum "${BASE}.tar.gz.part-"* > "${BASE}.sha256"
)

echo
echo "Done."
echo "Parts:"
ls -lh "$OUT_DIR/${BASE}.tar.gz.part-"*
echo "Checksums: $OUT_DIR/${BASE}.sha256"
echo
echo "Restore commands:"
echo "  cat ${BASE}.tar.gz.part-* > ${BASE}.tar.gz"
echo "  sha256sum -c ${BASE}.sha256"
echo "  tar -xzf ${BASE}.tar.gz"
