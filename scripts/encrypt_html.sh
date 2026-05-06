#!/usr/bin/env bash
# Encrypt docs/index.html with StatiCrypt for password-gated deployment.
#
#   bash scripts/encrypt_html.sh                # uses dev password FogAccess2026
#   SITE_PASSWORD=... bash scripts/encrypt_html.sh   # custom password
#
# After build_index.py rewrites docs/index.html in unencrypted form,
# run this to encrypt in place. CI workflows do the same via inline npx.
set -euo pipefail

PASSWORD="${SITE_PASSWORD:-FogAccess2026}"

cd "$(dirname "$0")/.."

if [ ! -f docs/index.html ]; then
  echo "docs/index.html missing — run python scripts/build_index.py first" >&2
  exit 1
fi

# If the file is already encrypted, decrypt first so re-encryption picks
# up any in-flight changes from the source.
if grep -q "staticrypt-html" docs/index.html; then
  npx --yes staticrypt --decrypt docs/index.html -p "$PASSWORD" --short -d docs
fi

npx --yes staticrypt docs/index.html \
  -p "$PASSWORD" \
  --short \
  --template-title "FOG Industry Command Center" \
  --template-instructions "Enter the access password to continue." \
  --template-color-primary "#1F3864" \
  --template-color-secondary "#3498db" \
  --template-button "Access Command Center" \
  --remember 30 \
  -d docs

echo "Encrypted docs/index.html ($(wc -c < docs/index.html | tr -d ' ') bytes)"
