#!/usr/bin/env bash
set -euo pipefail

# pushup_release.sh
# 1) Package dataset artifacts into split archives
# 2) Create/update a GitHub release for a tag
# 3) Upload archive parts + checksum file as release assets
#
# Usage:
#   scripts/pushup_release.sh [tag] [output_dir] [chunk_size] [repo_slug]
#
# Defaults:
#   tag        = dataset_YYYYMMDD_HHMMSS
#   output_dir = /opt/tmp_share/scottsa/GNN
#   chunk_size = 1900m
#   repo_slug  = derived from origin remote (owner/repo)

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

TAG="${1:-dataset_$(date +%Y%m%d_%H%M%S)}"
OUT_DIR="${2:-/opt/tmp_share/scottsa/GNN}"
CHUNK_SIZE="${3:-1900m}"

derive_repo_slug() {
  local remote_url
  remote_url="$(git remote get-url origin)"
  python3 - "$remote_url" <<'PY'
import re
import sys

u = sys.argv[1].strip()
m = re.match(r"^https://github\.com/([^/]+)/(.+?)(?:\.git)?$", u)
if not m:
    m = re.match(r"^git@github\.com:([^/]+)/(.+?)(?:\.git)?$", u)
if not m:
    raise SystemExit(f"Could not parse GitHub repo slug from remote: {u}")
print(f"{m.group(1)}/{m.group(2)}")
PY
}

REPO_SLUG="${4:-$(derive_repo_slug)}"
BASE="gnn_sta_dataset_${TAG}"
ARCHIVE="${OUT_DIR}/${BASE}.tar.gz"
SHA_FILE="${OUT_DIR}/${BASE}.sha256"

echo "[pushup] repo=${REPO_SLUG} tag=${TAG}"
echo "[pushup] packaging dataset into ${OUT_DIR}"
scripts/package_dataset_release.sh "$OUT_DIR" "$TAG" "$CHUNK_SIZE"

if [[ ! -f "$SHA_FILE" ]]; then
  echo "[pushup] missing checksum file: $SHA_FILE" >&2
  exit 1
fi

mapfile -t PARTS < <(ls -1 "${OUT_DIR}/${BASE}.tar.gz.part-"* 2>/dev/null || true)
if [[ "${#PARTS[@]}" -eq 0 ]]; then
  echo "[pushup] no archive parts found for ${BASE}" >&2
  exit 1
fi

read_git_credentials() {
  local creds
  creds="$(printf "protocol=https\nhost=github.com\n\n" | git credential fill || true)"
  GH_USER="$(printf '%s\n' "$creds" | sed -n 's/^username=//p' | head -n 1)"
  GH_PASS="$(printf '%s\n' "$creds" | sed -n 's/^password=//p' | head -n 1)"
  if [[ -z "${GH_USER:-}" || -z "${GH_PASS:-}" ]]; then
    echo "[pushup] failed to read GitHub credentials from git credential store." >&2
    echo "[pushup] configure credentials first (git credential-store)." >&2
    exit 1
  fi
}

urlencode() {
  python3 - "$1" <<'PY'
import sys
import urllib.parse
print(urllib.parse.quote(sys.argv[1], safe=""))
PY
}

read_git_credentials
AUTH="${GH_USER}:${GH_PASS}"
API_BASE="https://api.github.com/repos/${REPO_SLUG}"
HTTP_HEADERS=(
  -H "Accept: application/vnd.github+json"
  -H "X-GitHub-Api-Version: 2022-11-28"
)

tmp_release_json="$(mktemp)"
trap 'rm -f "$tmp_release_json"' EXIT

echo "[pushup] checking release tag ${TAG}"
status_code="$(curl -sS -u "$AUTH" "${HTTP_HEADERS[@]}" -o "$tmp_release_json" -w "%{http_code}" \
  "${API_BASE}/releases/tags/${TAG}")"

if [[ "$status_code" == "404" ]]; then
  echo "[pushup] creating release ${TAG}"
  payload="$(python3 - "$TAG" "$BASE" <<'PY'
import json
import sys

tag = sys.argv[1]
base = sys.argv[2]
body = (
    f"Automated dataset release for `{base}`.\n\n"
    "- Contains: data/raw_curated, data/processed, data/manifests, logs/pipeline\n"
    "- Format: split tar.gz parts + sha256 file\n"
)
print(json.dumps({
    "tag_name": tag,
    "name": tag,
    "body": body,
    "draft": False,
    "prerelease": False
}))
PY
)"
  curl -sS -u "$AUTH" "${HTTP_HEADERS[@]}" \
    -X POST "${API_BASE}/releases" \
    -d "$payload" > "$tmp_release_json"
elif [[ "$status_code" != "200" ]]; then
  echo "[pushup] GitHub API failed while checking release. HTTP ${status_code}" >&2
  cat "$tmp_release_json" >&2
  exit 1
else
  echo "[pushup] release ${TAG} already exists; assets will be replaced if needed"
fi

RELEASE_ID="$(python3 - "$tmp_release_json" <<'PY'
import json
import sys
data = json.load(open(sys.argv[1], "r", encoding="utf-8"))
print(data.get("id", ""))
PY
)"
UPLOAD_URL="$(python3 - "$tmp_release_json" <<'PY'
import json
import sys
data = json.load(open(sys.argv[1], "r", encoding="utf-8"))
url = data.get("upload_url", "")
print(url.split("{", 1)[0])
PY
)"

if [[ -z "$RELEASE_ID" || -z "$UPLOAD_URL" ]]; then
  echo "[pushup] failed to resolve release id/upload url" >&2
  cat "$tmp_release_json" >&2
  exit 1
fi

delete_asset_if_exists() {
  local asset_name="$1"
  local assets_json asset_id
  assets_json="$(mktemp)"
  curl -sS -u "$AUTH" "${HTTP_HEADERS[@]}" \
    "${API_BASE}/releases/${RELEASE_ID}/assets?per_page=100" > "$assets_json"
  asset_id="$(python3 - "$assets_json" "$asset_name" <<'PY'
import json
import sys

items = json.load(open(sys.argv[1], "r", encoding="utf-8"))
name = sys.argv[2]
for item in items:
    if item.get("name") == name:
        print(item.get("id", ""))
        break
PY
)"
  rm -f "$assets_json"
  if [[ -n "$asset_id" ]]; then
    echo "[pushup] deleting existing asset ${asset_name}"
    curl -sS -u "$AUTH" "${HTTP_HEADERS[@]}" \
      -X DELETE "${API_BASE}/releases/assets/${asset_id}" > /dev/null
  fi
}

upload_asset() {
  local asset_path="$1"
  local asset_name encoded_name
  asset_name="$(basename "$asset_path")"
  encoded_name="$(urlencode "$asset_name")"
  delete_asset_if_exists "$asset_name"
  echo "[pushup] uploading ${asset_name}"
  curl -sS -u "$AUTH" \
    -H "Content-Type: application/octet-stream" \
    --data-binary @"$asset_path" \
    "${UPLOAD_URL}?name=${encoded_name}" > /dev/null
}

for part in "${PARTS[@]}"; do
  upload_asset "$part"
done
upload_asset "$SHA_FILE"

echo "[pushup] release upload complete"
echo "[pushup] release URL: https://github.com/${REPO_SLUG}/releases/tag/${TAG}"
echo "[pushup] asset base: ${BASE}"
echo "[pushup] local archive: ${ARCHIVE}"
