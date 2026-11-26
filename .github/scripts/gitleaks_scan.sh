#!/usr/bin/env bash
# Suppression surfaces come from BASE-REF, never the scanned tree.
set -euo pipefail
(( $# == 1 )) || { echo "usage: gitleaks_scan.sh BASE-REF" >&2; exit 2; }
base=$1
temp=${RUNNER_TEMP:-${TMPDIR:-/tmp}}
config="$temp/gitleaks.toml"
remote="refs/remotes/origin/${base}"
if ! git fetch --no-tags --quiet origin "+refs/heads/${base}:${remote}" 2>"$temp/fetch-error"; then
  # A missing base branch is a configuration error; anything else may be transport.
  if grep -q "couldn't find remote ref" "$temp/fetch-error"; then
    cat "$temp/fetch-error" >&2
    exit 1
  fi
  "$(dirname "$0")/retry.sh" git fetch --no-tags --quiet origin "+refs/heads/${base}:${remote}"
fi
if git show "${remote}:.gitleaks.toml" > "$config" 2>/dev/null \
    && grep -q '^\[extend\]' "$config" \
    && grep -qE '^useDefault[[:space:]]*=[[:space:]]*true$' "$config"; then
  :
else
  printf '[extend]\nuseDefault = true\n' > "$config"
fi
rm -f -- .gitleaksignore
git show "${remote}:.gitleaksignore" > .gitleaksignore 2>/dev/null || rm -f -- .gitleaksignore
printf '* diff\n' > "$(git rev-parse --git-path info/attributes)"
./gitleaks detect --verbose --redact --ignore-gitleaks-allow --config "$config" \
  --log-opts='--full-history --all --diff-filter=uxdb --diff-merges=first-parent'
