#!/usr/bin/env bash
# Contract: tests/test_ci_retry_helper.py
attempt=1
attempts=3
delays=(5 10)
out="$(mktemp)" || exit 1
trap 'rm -f "$out"' EXIT
while :; do
  status=0
  "$@" >"$out" || status=$?
  if [ "$status" -eq 0 ]; then
    cat "$out"
    exit
  fi
  if [ "$attempt" -ge "$attempts" ]; then
    echo "retry.sh: $* still failing after $attempts attempts" >&2
    exit "$status"
  fi
  echo "retry.sh: $* failed (attempt $attempt of $attempts); retrying" >&2
  sleep "${delays[$((attempt - 1))]}"
  attempt=$((attempt + 1))
done
