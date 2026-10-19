#!/usr/bin/env bash
# Copy the repo's tools/ into this add-on folder so a Home Assistant LOCAL add-on
# (build context = this folder) can COPY them. Re-run after changing tools/.
# This keeps tools/ single-source in the repo; the vendored copy is git-ignored.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"

rm -rf "$HERE/tools"
cp -R "$REPO/tools" "$HERE/tools"
# Drop caches; the container installs its own slim deps.
find "$HERE/tools" -name '__pycache__' -type d -prune -exec rm -rf {} +

echo "Vendored tools/ into $HERE/tools"
echo "Next: copy this folder to your HA /addons/ share, then install it from"
echo "Settings -> Add-ons -> Add-on Store -> (three dots) -> Local add-ons."
