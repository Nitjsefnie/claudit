"""The stall-bound rules of the workflow fetch guard, one row per limb.

A retried attempt must end when the network goes silent, or it is never
retried. A gh command run by retry.sh/api_retry, or a gh read steering an
attempt loop, fails unless it is `timeout -k N N gh ...` there: timeout
directly before gh, inside the helper, each N whole seconds above 0. The
`--` that ends a prefix command's options is skipped, so the command after
it is judged as without it.

A git fetch/clone/ls-remote/pull/push is judged by every word after the
subcommand that is shaped like a remote, read as git reads one (GIT URLS in
git-fetch(1)). An option word is read by the value after its `=`; a word
starting with `$` is a variable and has no shape. `http://` and `https://`
are http and need `-c http.lowSpeedLimit=N -c http.lowSpeedTime=N` (above 0;
of a repeated name the last counts, as git reads it). `ssh://`, `git+ssh://`,
`ssh+git://` and the scp-like `user@host:path` (a colon and an `@` with no
slash before them) are ssh, as is `host:path` when it is the first word that
is no option. ssh needs an earlier
`export GIT_SSH_COMMAND='ssh -o Name=value ...'` in the run whose first
ConnectTimeout and first ServerAliveInterval are above 0, as is its first
ServerAliveCountMax when given (OpenSSH uses the first of a repeated
option), and never passes on the pair. Any other `scheme://` word or `::`
address fails: nothing here bounds it. Words of two transports fail, both
named: one is an option's value, to be written `--option=value`. With no
shaped word (a name, a variable, a path) either bound passes. Any other
mention of GIT_SSH_COMMAND fails.

Not read: `env:`, GIT_HTTP_LOW_SPEED_*, `--config-env`, `url.*.insteadOf`,
the transport behind a name or variable, a `host:path` with no user that
follows an option's separate value or is joined to an option, and control
flow: an export that is its own command (not directly after a shell
keyword) counts for every command written after it in the run, whatever
branch, function or subshell holds it. pip, playwright, curl and other
uses of timeout keep their rules.

Each refused row differs from an accepted one in exactly one thing. The
guard's own test runs the same rules over every real workflow."""
from __future__ import annotations

import pytest

from tests.test_workflow_fetch_retry import GIT_REMOTE_CALLS, _judged

COPY = '"$RUNNER_TEMP/retry.sh" '
READ = 'gh api "repos/$CLAUDIT_REPO/commits/$CLAUDIT_SHA" --jq .sha'
PATCH = 'gh api -X PATCH "repos/$CLAUDIT_REPO/issues/comments/$id" -F body=@comment.md'
LOOP = "for attempt in 1 2 3; do if {}; then break; fi; sleep 5; done"
URL = '"https://github.com/${CLAUDIT_REPO}.git" "refs/heads/${CLAUDIT_BASE_REF}"'
PAIR = "-c http.lowSpeedLimit=1000 -c http.lowSpeedTime=60 "
BOUNDS = "-o ConnectTimeout=30 -o ServerAliveInterval=15 -o ServerAliveCountMax=4"
FETCH = COPY + 'git -C "$RUNNER_TEMP/push-repo" fetch --quiet origin master'

UNBOUNDED_READ = "gh read retried without `timeout -k N N gh`, N above 0, per attempt"
UNBOUNDED_WRITE = "gh write retried without `timeout -k N N gh`, N above 0, per attempt"
HTTPS = "https://github.com/${CLAUDIT_REPO}.git"
OVER_HTTP = f"git fetch over http(s) (`{HTTPS}`) without `-c http.lowSpeedLimit=N -c http.lowSpeedTime=N`, N above 0"
NAMED = ("git fetch to a remote that is a name, a variable or a path, with neither the http "
         "low-speed pair nor a bounded GIT_SSH_COMMAND export")
UNREAD_SSH = ("GIT_SSH_COMMAND not exported as `ssh -o Name=value ...` with "
              "ConnectTimeout and ServerAliveInterval above 0")
SSH_URL = "ssh://git@example.com/o/r.git"
DEPLOY = "deploy@example.com:o/r.git"


def _over_ssh(word: str) -> str:
    return f"git fetch over ssh (`{word}`) without an earlier `export GIT_SSH_COMMAND=` bounding connect and keepalive"


def _uncovered(word: str) -> str:
    return f"git fetch to `{word}`, neither http(s) nor ssh, which no stall bound covers"


def _two(first: str, second: str) -> str:
    return f"git fetch with words of two transports, {first} and {second}; write an option's value as --option=value"


def _ssh(options: str = "-o StrictHostKeyChecking=yes " + BOUNDS, quote: str = "'") -> str:
    return f"export GIT_SSH_COMMAND={quote}ssh {options}{quote}\n"


def _reasons(script: str) -> list[str]:
    return [reason for _, _, reasons in _judged(script) for reason in reasons]


ACCEPTED = {
    "gh read by the helper copy": COPY + "timeout -k 5 60 " + READ,
    "gh read by the checkout's helper": "scripts/ci/retry.sh timeout -k 5 60 " + READ,
    "gh read by api_retry": 'api_retry "reading the head" -- timeout -k 5 60 ' + READ,
    "captured gh probe": f'if probe="$({COPY}timeout -k 5 60 {READ} 2>&1)"; then :; fi',
    "gh write by api_retry": 'api_retry "updating" -- timeout -k 5 60 ' + PATCH,
    "gh read steering a loop": LOOP.format("timeout -k 5 60 " + READ),
    "-- before gh": COPY + "timeout -k 5 60 -- " + READ,
    "-- before the duration": COPY + "timeout -k 5 -- 60 " + READ,
    "gh by its path": COPY + "timeout -k 5 60 /usr/bin/" + READ,
    "gh read in an inner shell": f"bash -c '{COPY}timeout -k 5 60 gh api repos/o/r'",
    "gh write steering a loop": LOOP.format(PATCH),
    "gh write nothing retries": 'gh api -X POST "repos/$CLAUDIT_REPO/issues/1/comments" -F body=@c.md',
    "gh release nothing retries": 'gh release create "$CLAUDIT_TAG" --generate-notes',
    "https fetch with the pair": LOOP.format(f"git {PAIR}fetch --no-tags {URL}"),
    "pair after another global option": LOOP.format(f'git -C "$dir" {PAIR}fetch origin'),
    "pair names in another case": LOOP.format(
        f"git -c HTTP.LOWSPEEDLIMIT=1000 -c http.lowspeedtime=60 fetch --no-tags {URL}"),
    "ssh fetch after the export": _ssh() + FETCH,
    "ssh option names in another case": _ssh(BOUNDS.replace("ServerAlive", "SERVERALIVE")) + FETCH,
    "ssh without a count": _ssh("-o ConnectTimeout=30 -o ServerAliveInterval=15") + FETCH,
    "ssh fetch in an inner shell": _ssh() + f"bash -c '{COPY}git fetch origin master'",
    "https fetch with the pair after the export": _ssh() + COPY + f"git {PAIR}fetch {URL}",
    "ssh URL after the export": _ssh() + COPY + "git fetch " + SSH_URL,
    "git+ssh URL after the export": _ssh() + COPY + "git fetch git+" + SSH_URL,
    "ssh+git URL after the export": _ssh() + COPY + "git fetch ssh+git" + SSH_URL[3:],
    "scp-like remote after the export": _ssh() + COPY + "git fetch deploy@example.com:o/r.git",
    "variable remote after the export": _ssh() + COPY + 'git ls-remote --exit-code "$remote" refs/heads/master',
    "push with a refspec after the export": _ssh() + COPY + 'git -C "$dir" push origin HEAD:master',
    "pair on a named remote, by the helper": COPY + f"git {PAIR}fetch origin",
    "option value joined with =, https remote": LOOP.format(f"git {PAIR}fetch --filter=blob:none {URL}"),
    "variable with a default, the pair": COPY + f'git {PAIR}fetch "${{REMOTE:-origin}}"',
    "flag before a named remote": COPY + f"git {PAIR}fetch --no-tags origin",
    "refspec with a colon on a named remote": COPY + f"git {PAIR}fetch origin master:master",
    "named remote behind an option's value": COPY + f"git {PAIR}fetch --upload-pack /opt/upload-pack origin",
    "path with a colon after a slash": COPY + f"git {PAIR}fetch ./mirror:1.git",
    "pair on a named remote": LOOP.format(f"git {PAIR}fetch origin master"),
    "local git commands": 'git init -q "$dir"\ngit -C "$dir" checkout -B push FETCH_HEAD',
    "pip by the helper": "scripts/ci/retry.sh pip install -r backend/requirements.txt",
    "playwright by the helper": "scripts/ci/retry.sh npx --no-install playwright install chromium",
    "curl with its own bounds": "curl -fsSL --max-time 60 --retry 3 --retry-all-errors -o f https://example.com/f",
    "timeout around other commands": 'timeout 120 docker pull "$ref"\nsudo timeout 10s rm -f /etc/f',
}

REFUSED = {
    "no timeout": (COPY + READ, [UNBOUNDED_READ]),
    "no timeout, api_retry": ('api_retry "reading the head" -- ' + READ, [UNBOUNDED_READ]),
    "no timeout on a write": ('api_retry "updating" -- ' + PATCH, [UNBOUNDED_WRITE]),
    "no timeout in a loop": (LOOP.format(READ), [UNBOUNDED_READ]),
    "no timeout in an inner shell": (f"bash -c '{COPY}gh api repos/o/r'", [UNBOUNDED_READ]),
    "duration 0": (COPY + "timeout -k 5 0 " + READ, [UNBOUNDED_READ]),
    "kill-after 0": (COPY + "timeout -k 0 60 " + READ, [UNBOUNDED_READ]),
    "no kill-after": (COPY + "timeout 60 " + READ, [UNBOUNDED_READ]),
    "duration 0 before --": (COPY + "timeout -k 5 0 -- " + READ, [UNBOUNDED_READ]),
    "no kill-after before --": (COPY + "timeout 60 -- " + READ, [UNBOUNDED_READ]),
    "timeout -- around something else": (COPY + "timeout -k 5 60 -- env " + READ, [UNBOUNDED_READ]),
    "timeout -- and no retry": ("timeout -k 5 60 -- " + READ, ["gh read without the retry helper"]),
    "env -- and no timeout": (COPY + "env -- " + READ, [UNBOUNDED_READ]),
    "git behind timeout --, no retry": ("timeout 60 -- git fetch origin", ["git fetch without the retry helper", NAMED]),
    "duration with a unit": (COPY + "timeout -k 5 60s " + READ, [UNBOUNDED_READ]),
    "duration in a variable": (COPY + 'timeout -k 5 "$LIMIT" ' + READ, [UNBOUNDED_READ]),
    "duration in other digits": (COPY + "timeout -k 5 ６０ " + READ, [UNBOUNDED_READ]),
    "an option the guard does not read": (COPY + "timeout -s 9 60 " + READ, [UNBOUNDED_READ]),
    "timeout outside the helper": ("timeout -k 5 60 " + COPY + READ, [UNBOUNDED_READ]),
    "timeout around something else": (COPY + "timeout -k 5 60 env " + READ, [UNBOUNDED_READ]),
    "timeout and no retry": ("timeout -k 5 60 " + READ, ["gh read without the retry helper"]),
    "https fetch without the pair": (LOOP.format(f"git fetch --no-tags {URL}"), [OVER_HTTP]),
    "only the limit": (LOOP.format(f"git -c http.lowSpeedLimit=1000 fetch --no-tags {URL}"),
                       [OVER_HTTP]),
    "only the time": (LOOP.format(f"git -c http.lowSpeedTime=60 fetch --no-tags {URL}"),
                      [OVER_HTTP]),
    "limit 0": (LOOP.format(f"git -c http.lowSpeedLimit=0 -c http.lowSpeedTime=60 fetch {URL}"),
                [OVER_HTTP]),
    "time 0": (LOOP.format(f"git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=0 fetch {URL}"),
               [OVER_HTTP]),
    "time overridden by a later 0": (
        LOOP.format(f"git {PAIR}-c http.lowSpeedTime=0 fetch --no-tags {URL}"), [OVER_HTTP]),
    "time in a variable": (
        LOOP.format(f'git -c http.lowSpeedLimit=1000 -c "http.lowSpeedTime=$WINDOW" fetch {URL}'),
        [OVER_HTTP]),
    "time with a unit": (
        LOOP.format(f"git -c http.lowSpeedLimit=1k -c http.lowSpeedTime=60 fetch {URL}"), [OVER_HTTP]),
    "https fetch under the export alone": (_ssh() + COPY + f"git fetch {URL}", [OVER_HTTP]),
    "http fetch under the export alone": (
        _ssh() + COPY + "git fetch http://example.com/o/r.git master",
        [OVER_HTTP.replace(HTTPS, "http://example.com/o/r.git")]),
    "ssh URL with the pair alone": (COPY + f"git {PAIR}fetch {SSH_URL}", [_over_ssh(f"{SSH_URL}")]),
    "git+ssh URL with the pair alone": (COPY + f"git {PAIR}fetch git+{SSH_URL}", [_over_ssh(f"git+{SSH_URL}")]),
    "ssh+git URL with the pair alone": (COPY + f"git {PAIR}fetch ssh+git{SSH_URL[3:]}", [_over_ssh(f"ssh+git{SSH_URL[3:]}")]),
    "git@ remote with the pair alone": (COPY + f"git {PAIR}fetch git@example.com:o/r.git", [_over_ssh("git@example.com:o/r.git")]),
    "scp-like remote with the pair alone": (
        COPY + f"git {PAIR}fetch deploy@example.com:o/r.git", [_over_ssh("deploy@example.com:o/r.git")]),
    "scp-like remote after a flag, the pair alone": (
        COPY + f"git {PAIR}fetch --no-tags deploy@example.com:o/r.git", [_over_ssh("deploy@example.com:o/r.git")]),
    "host:path after a flag, the pair alone": (
        COPY + f"git {PAIR}fetch --no-tags example.com:o/r.git", [_over_ssh("example.com:o/r.git")]),
    "scp-like remote without a user, the pair alone": (
        COPY + f"git {PAIR}fetch example.com:o/r.git", [_over_ssh("example.com:o/r.git")]),
    "ssh URL behind an option's value, the pair alone": (
        COPY + f"git {PAIR}fetch --upload-pack /opt/upload-pack {SSH_URL}", [_over_ssh(f"{SSH_URL}")]),
    "git URL with the pair": (COPY + f"git {PAIR}fetch git://example.com/o/r.git", [_uncovered("git://example.com/o/r.git")]),
    "git URL after the export": (_ssh() + COPY + "git fetch git://example.com/o/r.git", [_uncovered("git://example.com/o/r.git")]),
    "file URL after the export": (_ssh() + COPY + "git fetch file:///srv/o/r.git", [_uncovered("file:///srv/o/r.git")]),
    "helper address after the export": (_ssh() + COPY + "git fetch helper::example.com/o/r.git",
                                        [_uncovered("helper::example.com/o/r.git")]),
    "two transports, both bounds": (
        _ssh() + COPY + f"git {PAIR}fetch {URL} --reference {SSH_URL}",
        [_two(f"`{HTTPS}` (http)", f"`{SSH_URL}` (ssh)")]),
    "option value with a colon before an https remote": (
        LOOP.format(f"git {PAIR}fetch --filter blob:none {URL}"), [_two("`blob:none` (ssh)", f"`{HTTPS}` (http)")]),
    "option value with a colon before a named remote": (
        LOOP.format(f"git {PAIR}fetch --filter blob:none origin"), [_over_ssh("blob:none")]),
    "git@ remote behind an option's value, the pair alone": (
        COPY + f"git {PAIR}fetch --depth 1 git@example.com:o/r.git", [_over_ssh("git@example.com:o/r.git")]),
    "user@host remote behind an option's value, the pair alone": (
        COPY + f"git {PAIR}fetch --depth 1 {DEPLOY}", [_over_ssh(DEPLOY)]),
    "ssh URL joined to an option, the pair alone": (
        COPY + f"git {PAIR}fetch --upload-pack={SSH_URL} origin", [_over_ssh(f"--upload-pack={SSH_URL}")]),
    "scheme in capitals, the pair": (
        COPY + f"git {PAIR}fetch HTTPS://example.com/o/r.git", [_uncovered("HTTPS://example.com/o/r.git")]),
    "ssh fetch without the export": (FETCH, [NAMED]),
    "export after the fetch": (FETCH + "\n" + _ssh(), [NAMED]),
    "fetch in an inner shell, no export": (f"bash -c '{COPY}git fetch origin master'", [NAMED]),
    "connect bound only": (_ssh("-o StrictHostKeyChecking=yes -o ConnectTimeout=30") + FETCH,
                           [UNREAD_SSH, NAMED]),
    "liveness bound only": (_ssh("-o ServerAliveInterval=15 -o ServerAliveCountMax=4") + FETCH,
                            [UNREAD_SSH, NAMED]),
    "connect bound 0": (_ssh(BOUNDS.replace("=30", "=0")) + FETCH, [UNREAD_SSH, NAMED]),
    "interval 0": (_ssh(BOUNDS.replace("=15", "=0")) + FETCH, [UNREAD_SSH, NAMED]),
    "count 0": (_ssh(BOUNDS.replace("=4", "=0")) + FETCH, [UNREAD_SSH, NAMED]),
    "connect bound after an earlier 0": (_ssh("-o ConnectTimeout=0 " + BOUNDS) + FETCH,
                                         [UNREAD_SSH, NAMED]),
    "interval after an earlier 0": (_ssh("-o ServerAliveInterval=0 " + BOUNDS) + FETCH,
                                    [UNREAD_SSH, NAMED]),
    "bound inside another option's value": (
        _ssh('-o ProxyCommand="nc -o ConnectTimeout=30 %h" -o ServerAliveInterval=15') + FETCH,
        [UNREAD_SSH, NAMED]),
    "bounds as the values of other flags": (
        _ssh("-i ConnectTimeout=30 -l ServerAliveInterval=15") + FETCH, [UNREAD_SSH, NAMED]),
    "option glued to its flag": (_ssh("-oConnectTimeout=30 -oServerAliveInterval=15") + FETCH,
                                 [UNREAD_SSH, NAMED]),
    "option without a value": (_ssh("-o ConnectTimeout -o ServerAliveInterval=15") + FETCH,
                               [UNREAD_SSH, NAMED]),
    "another ssh flag": (_ssh("-v " + BOUNDS) + FETCH, [UNREAD_SSH, NAMED]),
    "bound in a variable": (_ssh(BOUNDS.replace("=30", "=$CONNECT"), '"') + FETCH,
                            [UNREAD_SSH, NAMED]),
    "options in a variable": (_ssh("$SSH_OPTIONS " + BOUNDS, '"') + FETCH, [UNREAD_SSH, NAMED]),
    "a second command in the value": (_ssh(BOUNDS + "; true") + FETCH, [UNREAD_SSH, NAMED]),
    "a value the lexer cannot read": (_ssh(BOUNDS + " -o 'x", '"') + FETCH,
                                      [UNREAD_SSH, NAMED]),
    "not ssh": (_ssh().replace("'ssh ", "'plink ") + FETCH, [UNREAD_SSH, NAMED]),
    "exported empty": ("export GIT_SSH_COMMAND=\n" + FETCH, [UNREAD_SSH, NAMED]),
    "exported under a longer name": (_ssh().replace("GIT_SSH", "MY_GIT_SSH") + FETCH,
                                     [UNREAD_SSH, NAMED]),
    "declared, not exported": (_ssh().replace("export ", "readonly ") + FETCH,
                               [UNREAD_SSH, NAMED]),
    "exported twice on one line": (
        _ssh().rstrip("\n") + " GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=yes'\n" + FETCH,
        [UNREAD_SSH, NAMED]),
    "assigned, not exported": (_ssh().replace("export ", "") + FETCH, [UNREAD_SSH, NAMED]),
    "set on the command": (_ssh().replace("export ", "").rstrip("\n") + " " + FETCH,
                           [NAMED, UNREAD_SSH]),
    "exported beside another name": (_ssh().replace("export ", "export LANG=C ") + FETCH,
                                     [UNREAD_SSH, NAMED]),
    "unset after the export": (_ssh() + "unset GIT_SSH_COMMAND\n" + FETCH, [UNREAD_SSH]),
    "read after the export": (_ssh() + 'echo "$GIT_SSH_COMMAND"\n' + FETCH, [UNREAD_SSH]),
    "pip without the helper": ("pip install -r backend/requirements.txt",
                               ["pip install without the retry helper"]),
    "curl without a time limit": ("curl -fsSL --retry 3 --retry-all-errors -o f https://example.com/f",
                                  ["curl without --retry N, --retry-all-errors and --max-time"]),
}


@pytest.mark.parametrize("script", ACCEPTED.values(), ids=ACCEPTED)
def test_a_bounded_or_unaffected_shape_passes(script):
    assert _reasons(script) == []


@pytest.mark.parametrize("script,reasons", REFUSED.values(), ids=REFUSED)
def test_a_shape_without_its_stall_bound_is_refused(script, reasons):
    assert _reasons(script) == reasons


@pytest.mark.parametrize("verb", sorted(GIT_REMOTE_CALLS))
def test_every_git_network_command_needs_a_stall_bound(verb):
    command = f"{COPY}git {verb} origin master"
    assert _reasons(command) == [NAMED.replace("fetch", verb)]
    assert _reasons(_ssh() + command) == []
