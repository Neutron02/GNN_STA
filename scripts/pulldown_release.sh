#!/usr/bin/env bash
set -euo pipefail

# pulldown_release.sh
# 1) Download dataset release assets from GitHub by tag
# 2) Reconstruct archive from split parts
# 3) Verify checksum and extract
#
# Usage:
#   scripts/pulldown_release.sh <tag> [extract_dir] [repo_slug] [work_dir]
#
# Defaults:
#   extract_dir = /opt/tmp_share/scottsa/GNN
#   repo_slug   = derived from origin remote (owner/repo)
#   work_dir    = <extract_dir>/downloads/<tag>

if [[ $# -lt 1 ]]; then
  echo "Usage: scripts/pulldown_release.sh <tag> [extract_dir] [repo_slug] [work_dir]" >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

TAG="$1"
EXTRACT_DIR="${2:-/opt/tmp_share/scottsa/GNN}"

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

REPO_SLUG="${3:-$(derive_repo_slug)}"
WORK_DIR="${4:-${EXTRACT_DIR}/downloads/${TAG}}"
BASE="gnn_sta_dataset_${TAG}"
ARCHIVE="${WORK_DIR}/${BASE}.tar.gz"
SHA_FILE="${WORK_DIR}/${BASE}.sha256"

mkdir -p "$WORK_DIR" "$EXTRACT_DIR"

read_git_credentials() {
  local creds
  creds="$(printf "protocol=https\nhost=github.com\n\n" | git credential fill || true)"
  GH_USER="$(printf '%s\n' "$creds" | sed -n 's/^username=//p' | head -n 1)"
  GH_PASS="$(printf '%s\n' "$creds" | sed -n 's/^password=//p' | head -n 1)"
  if [[ -z "${GH_USER:-}" || -z "${GH_PASS:-}" ]]; then
    echo "[pulldown] failed to read GitHub credentials from git credential store." >&2
    echo "[pulldown] configure credentials first (git credential-store)." >&2
    exit 1
  fi
}

read_git_credentials
AUTH="${GH_USER}:${GH_PASS}"
API_BASE="https://api.github.com/repos/${REPO_SLUG}"
HTTP_HEADERS=(
  -H "Accept: application/vnd.github+json"
  -H "X-GitHub-Api-Version: 2022-11-28"
)

release_json="$(mktemp)"
trap 'rm -f "$release_json"' EXIT

echo "[pulldown] fetching release metadata for ${TAG}"
status_code="$(curl -sS -u "$AUTH" "${HTTP_HEADERS[@]}" -o "$release_json" -w "%{http_code}" \
  "${API_BASE}/releases/tags/${TAG}")"
if [[ "$status_code" != "200" ]]; then
  echo "[pulldown] failed to fetch release tag ${TAG}. HTTP ${status_code}" >&2
  cat "$release_json" >&2
  exit 1
fi

mapfile -t ASSETS < <(python3 - "$release_json" "$BASE" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], "r", encoding="utf-8"))
base = sys.argv[2]
for item in data.get("assets", []):
    name = item.get("name", "")
    if name.startswith(base + ".tar.gz.part-") or name == base + ".sha256":
        print(name + "\t" + item.get("browser_download_url", ""))
PY
)

if [[ "${#ASSETS[@]}" -eq 0 ]]; then
  echo "[pulldown] no matching assets found for base ${BASE}" >&2
  exit 1
fi

echo "[pulldown] downloading assets to ${WORK_DIR}"
for row in "${ASSETS[@]}"; do
  name="${row%%$'\t'*}"
  url="${row#*$'\t'}"
  if [[ -z "$url" || "$url" == "$name" ]]; then
    echo "[pulldown] missing download url for asset ${name}" >&2
    exit 1
  fi
  echo "[pulldown] download ${name}"
  curl -L -sS -u "$AUTH" -o "${WORK_DIR}/${name}" "$url"
done

mapfile -t PARTS < <(ls -1 "${WORK_DIR}/${BASE}.tar.gz.part-"* 2>/dev/null || true)
if [[ "${#PARTS[@]}" -eq 0 ]]; then
  echo "[pulldown] no archive parts downloaded for ${BASE}" >&2
  exit 1
fi
if [[ ! -f "$SHA_FILE" ]]; then
  echo "[pulldown] missing checksum file ${SHA_FILE}" >&2
  exit 1
fi

echo "[pulldown] reconstruct archive ${ARCHIVE}"
cat "${WORK_DIR}/${BASE}.tar.gz.part-"* > "$ARCHIVE"

echo "[pulldown] verify checksums"
(
  cd "$WORK_DIR"
  sha256sum -c "$(basename "$SHA_FILE")"
)

echo "[pulldown] extract into ${EXTRACT_DIR}"
tar -xzf "$ARCHIVE" -C "$EXTRACT_DIR"

echo "[pulldown] done"
echo "[pulldown] extracted data under ${EXTRACT_DIR}"
echo "[pulldown] kept downloads under ${WORK_DIR}"
