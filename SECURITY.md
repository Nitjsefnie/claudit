# Security Policy

## Supported versions

claudit is self-hosted and rolling. Only the latest `master` is
supported: there are no release branches and no backports, and a
deployment updates by tracking `master`. If you are running an older
checkout, update before reporting — the problem may already be fixed.

## Reporting a vulnerability

Please do not open a public issue for a security report.

This repository has GitHub's private vulnerability reporting enabled.
Use it: open the repository's **Security** tab and choose **Report a
vulnerability**. Reports filed there are private — you can describe the
flaw without disclosing it publicly, and the report opens a private
thread where the fix can be discussed and developed before any public
disclosure.

When reporting, please include:

- what the vulnerability is and the impact you believe it has,
- how to reproduce it (the request, transcript, or configuration that
  triggers it),
- the commit your deployment runs (`/health` reports the version).

Once a fix is ready it lands on `master` like any other change, and the
reporter is credited in the advisory unless they prefer otherwise.
