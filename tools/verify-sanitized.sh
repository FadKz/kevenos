#!/usr/bin/env bash
set -euo pipefail

package_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
deny='pitchperfect[^ ]*hostme\.fyi|git\.tmflix\.com|llm\.infra\.tmflix\.com|tmadmin/kevenos|tmwhead/kelzia-brain|/etc/tmos|fadikelzia|Fadi Kelzia|Taj Dubai|Business Bay|Redington|Marina Key Realty|Al Waha Trading|Gulf Interiors Group|Jebel Freight Logistics|PitchPerfectAE_bot|ImprovMX'

if rg -n -i --hidden \
  --glob '!.git' \
  --glob '!.git/**' \
  --glob '!.venv/**' \
  --glob '!node_modules/**' \
  --glob '!MANIFEST.sha256' \
  --glob '!verify-sanitized.sh' \
  "$deny" "$package_dir"; then
  echo "sanitization check failed: a private or deployment-specific reference remains" >&2
  exit 1
fi

if find "$package_dir" \( -path "$package_dir/.git" -o -path "$package_dir/.venv" -o -path "$package_dir/node_modules" \) -prune -o -type f \( \
  -name '*.db' -o -name '*.sqlite*' -o -name '.env' -o -name '*.pem' \
  -o -name '*.key' -o -name '*.pdf' -o -name '*.jpg' -o -name '*.jpeg' \
  -o -name '*.mp4' \) -print -quit | grep -q .; then
  echo "sanitization check failed: a private/runtime/generated artifact remains" >&2
  exit 1
fi

echo "sanitization check passed"
