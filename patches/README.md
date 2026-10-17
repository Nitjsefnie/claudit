# Dependency patches

`v8-to-istanbul+9.3.0.patch` prevents v8-to-istanbul 9.3.0 from counting
comment-only and blank lines as covered executable lines. Without the patch,
changing comments can move the JavaScript line-coverage gate.

`npm ci` applies this patch through the `patch-package` postinstall script.
Remove it once an upstream release includes the fix.

Upstream issue: [istanbuljs/v8-to-istanbul#272](https://github.com/istanbuljs/v8-to-istanbul/issues/272).
Upstream PR: [istanbuljs/v8-to-istanbul#273](https://github.com/istanbuljs/v8-to-istanbul/pull/273).
