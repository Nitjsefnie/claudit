# Dependency patches

`v8-to-istanbul+9.3.0.patch` prevents v8-to-istanbul 9.3.0 from counting
comment-only and blank lines as covered executable lines. Without the patch,
changing comments can move the JavaScript line-coverage gate.

`npm ci` runs `python3 scripts/ci/apply_converter_patch.py` at postinstall.
The script uses the system `patch` command to apply the fix to the pinned
v8-to-istanbul 9.3.0 dependency and refuses version drift.

Remove this patch, the v8-to-istanbul overrides pin and the postinstall
script once [upstream PR #273](https://github.com/istanbuljs/v8-to-istanbul/pull/273)
ships in a release.

Upstream issue: [istanbuljs/v8-to-istanbul#272](https://github.com/istanbuljs/v8-to-istanbul/issues/272).
Upstream PR: [istanbuljs/v8-to-istanbul#273](https://github.com/istanbuljs/v8-to-istanbul/pull/273).
