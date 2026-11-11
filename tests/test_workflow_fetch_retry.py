"""Every network read and git push in a workflow or composite action retries transient failures.

Fails, listing each offending `run:` command, on a curl lacking `--retry N`
(N 1-9), `--retry-all-errors` or `--max-time`/`-m`; any wget; a git
fetch/clone/ls-remote/pull, gh read, pip install/download/wheel or
playwright install neither run by retry.sh/api_retry nor, inside a
`for attempt in` loop, steering it (an `if`/`elif`/`while`/`until`
condition, the left operand of `&&`, or of a `||` whose right side reaches
`continue`, bare or in a `{ }` group); a git push not run by
retry.sh/api_retry, even one steering such a loop; and an unclassified gh
command.

It also fails a gh API command, an npm command that may fetch and a git
network command that nothing ends when the network goes silent, and such an
npm command that nothing retries: test_workflow_stall_bounds.py states those
rules and what they do not read.

The reading is lexical: commands split on operators, newlines, `$( )`,
`<( )` and backticks. `bash`/`sh -c` strings (`-ec` too) are scripts, as are
heredocs on a line where bash/sh has no `-c` cluster or script operand;
other heredoc bodies are data.
It does not follow functions or aliases called by name, eval, a command in
a variable or a script file, or a `case` pattern inside `$( )`. A run it
cannot lex, or a non-bash shell, fails.
A step with no `shell:` reads as bash, even on Windows (pwsh there).
"""
from __future__ import annotations

import re
from pathlib import Path

from tests.workflow_checks import ROOT, WORKFLOWS, load_workflow

ACTIONS = ROOT / ".github" / "actions"
HELPERS = {"retry.sh", "api_retry"}
KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}",
            "time", "fi", "done", "esac"}
LOOPS = {"for", "while", "until", "select"}
LEADS = {"do", "then", "else", "!", "{"}
CONDITIONS = {"if", "elif", "while", "until"}
SHELLS = {"bash", "sh"}
SCRIPT_FLAG = re.compile(r"-[A-Za-z]*c[A-Za-z]*")
SHELL_VALUE_OPTIONS = {"--rcfile", "--init-file"}  # and a short cluster ending in o/O
PREFIXES = {"sudo": {"-u", "-g", "-C", "-D"}, "env": {"-u", "-C", "-S"},
            "timeout": {"-k", "-s"}, "nice": {"-n"}, "nohup": set(),
            "command": set(), "exec": set(), "stdbuf": set(),
            "xargs": {"-n", "-I", "-P", "-L", "-d", "-a", "-s", "-E"}}
GIT_VALUE_OPTIONS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
GIT_REMOTE_CALLS = {"fetch", "clone", "ls-remote", "pull", "push"}
LOW_SPEED = ("http.lowspeedlimit", "http.lowspeedtime")
GIT_SCHEMES = {"http": "http", "https": "http", "ssh": "ssh", "git+ssh": "ssh", "ssh+git": "ssh"}
GIT_BOUNDS = {
    "http": "over http(s) (`{}`) without `-c http.lowSpeedLimit=N -c http.lowSpeedTime=N`, N above 0",
    "ssh": "over ssh (`{}`) without an earlier `export GIT_SSH_COMMAND=` bounding connect and keepalive",
    "name": "to a remote that is a name, a variable or a path, with neither the http low-speed "
            "pair nor a bounded GIT_SSH_COMMAND export",
    "other": "to `{}`, neither http(s) nor ssh, which no stall bound covers"}
SSH_COMMAND = "GIT_SSH_COMMAND"
GH_LOCAL = {"version", "--version", "help", "--help"}
GH_READS = {"auth": {"status"},
            "release": {"view", "download", "list"},
            "run": {"view", "list", "download"},
            "pr": {"view", "list", "diff", "checks", "status"},
            "issue": {"view", "list", "status"}}
GH_WRITES = {"release": {"create", "upload", "edit", "delete"},
             "pr": {"create", "comment", "edit", "close", "merge", "reopen",
                    "ready", "review"},
             "issue": {"create", "comment", "edit", "close", "reopen", "delete"},
             "run": {"rerun", "cancel", "delete"},
             "workflow": {"run", "enable", "disable"}}
GH_API_VALUE_OPTIONS = {"-H", "--header", "-q", "--jq", "-t", "--template",
                        "-p", "--preview", "--hostname", "--cache"}
GH_API_FIELDS = ("-f", "-F", "--field", "--raw-field", "--input")
HTTP_METHODS = {"GET", "POST", "PATCH", "PUT", "DELETE", "HEAD"}
NPM_LOCAL = {"run", "run-script", "test"}
NPX_HOLDS = {"--no", "--no-yes", "--yes=false", "--offline", "--no-install"}
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\[[^]]*\])?\+?=")
SEPARATORS = ";&|\n"


class Unparseable(Exception):
    """A run shape the lexer does not read; the guard reports it."""


class _Lexer:  # pylint: disable=too-many-instance-attributes
    """Split one shell script into (words, inside an attempt loop) commands."""

    def __init__(self, text: str, in_attempt: bool = False) -> None:
        self.text, self.pos, self.in_attempt = text, 0, in_attempt
        self.commands: list[list] = []  # [words, in attempt loop, steers]
        self.condition, self.cmd_start, self.line_start = False, 0, 0
        self.or_left: tuple[int, int] | None = None
        self.or_depth = 0
        self.words: list[str] = []
        self.word: str | None = None
        self.loops: list[bool] = []
        self.heredocs: list[tuple[str, bool, bool, list[str]]] = []
        self.drop_next = False

    def run(self, closer: str = "") -> int:
        """Lex until `closer` (unquoted) or the end; returns the stop offset."""
        depth = 0
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char == closer and (closer != ")" or depth == 0):
                self.end_command()
                return self.pos
            if char in "()":
                depth += 1 if char == "(" else -1 if depth else 0
                self.end_command()
                self.pos += 1
            elif char in " \t":
                self.flush()
                self.pos += 1
            elif char in SEPARATORS:
                pair = self.text[self.pos:self.pos + 2]
                self.end_command(pair if pair in ("&&", "||") else "")
                self.pos += 2 if pair in ("&&", "||") else 1
                if char == "\n":
                    self.read_heredocs()
                    self.line_start = len(self.commands)
            elif char == "#" and self.word is None:
                newline = self.text.find("\n", self.pos)
                self.pos = len(self.text) if newline < 0 else newline
            elif char in "<>":
                self.redirect()
            else:
                self.word_char(quoted=False)
        if closer:
            raise Unparseable(f"unclosed {closer!r}")
        self.end_command()
        if self.loops or self.heredocs:
            raise Unparseable("unclosed loop or heredoc")
        return self.pos

    def word_char(self, quoted: bool) -> None:
        """Consume one word element: quote, escape, expansion or character."""
        text, pos = self.text, self.pos
        self.word = self.word or ""
        if text.startswith("$((", pos):
            end = text.find("))", pos)
            if end < 0 or "$(" in text[pos + 3:end] or "`" in text[pos:end]:
                raise Unparseable("arithmetic expansion")
            self.word += text[pos:end + 2]
            self.pos = end + 2
        elif text.startswith("$(", pos) or text[pos] == "`":
            self.nested(")" if text[pos] == "$" else "`", 2 if text[pos] == "$" else 1)
            self.word += "$SUB"
        elif text.startswith("${", pos):
            end = text.find("}", pos)
            if end < 0 or "$(" in text[pos:end] or "`" in text[pos:end]:
                raise Unparseable("parameter expansion")
            self.word += text[pos:end + 1]
            self.pos = end + 1
        elif text[pos] == "\\":
            self.word += text[pos + 1:pos + 2]
            self.pos += 2
        elif text[pos] == "'" and not quoted:
            end = text.find("'", pos + 1)
            if end < 0:
                raise Unparseable("unbalanced single quote")
            self.word += text[pos + 1:end]
            self.pos = end + 1
        elif text[pos] == '"' and not quoted:
            self.pos += 1
            while self.pos < len(text) and text[self.pos] != '"':
                self.word_char(quoted=True)
            if self.pos >= len(text):
                raise Unparseable("unbalanced double quote")
            self.pos += 1
        else:
            self.word += text[pos]
            self.pos += 1

    def nested(self, closer: str, skip: int) -> None:
        inner = _Lexer(self.text, self.in_attempt or any(self.loops))
        inner.pos = self.pos + skip
        self.pos = inner.run(closer) + 1
        self.commands += inner.commands

    def redirect(self) -> None:
        """Drop a redirection and its target; heredocs queue their body."""
        text, pos = self.text, self.pos
        if text[pos + 1:pos + 2] == "(":
            self.flush()
            self.nested(")", 2)
            return
        if self.word is not None and self.word.isdigit():
            self.word = None
        self.flush()
        operator = re.match(r"<<<|<<-?|[<>]+&?[-0-9]*|[<>]+", text[pos:])
        assert operator
        self.pos += len(operator[0])
        if operator[0] in ("<<", "<<-"):
            match = re.match(r"[ \t]*(['\"]?)\\?([A-Za-z_][A-Za-z0-9_]*)\1",
                             text[self.pos:])
            if not match:
                raise Unparseable("heredoc delimiter")
            self.pos += match.end()
            self.heredocs.append((match[2], bool(match[1]) or "\\" in match[0],
                                  operator[0] == "<<-", self.words))
        elif not operator[0].endswith(("&", "-")) and not operator[0][-1].isdigit():
            self.drop_next = True

    def read_heredocs(self) -> None:
        for delimiter, quoted, tabs, _ in self.heredocs:
            lines = self.text[self.pos:].split("\n")
            ends = [i for i, line in enumerate(lines)
                    if (line.lstrip("\t") if tabs else line) == delimiter]
            if not ends:
                raise Unparseable(f"unterminated heredoc {delimiter}")
            body = "\n".join(lines[:ends[0]])
            self.pos += sum(len(line) + 1 for line in lines[:ends[0] + 1])
            if any(_stdin_shell(entry[0]) for entry in self.commands[self.line_start:]):
                inner = _Lexer(body, self.in_attempt or any(self.loops))
                inner.run()
                self.commands += inner.commands
            elif not quoted and ("$(" in body or "`" in body):
                raise Unparseable(f"expanding heredoc {delimiter}")
        self.heredocs, self.cmd_start = [], len(self.commands)

    def or_right(self, words: list[str]) -> None:
        """Mark the `||` left operand steering once its right side reaches `continue`."""
        assert self.or_left
        body = _after(words, LEADS)
        self.or_depth += words[:len(words) - len(body)].count("{")
        if body[:1] == ["continue"]:
            for entry in self.commands[self.or_left[0]:self.or_left[1]]:
                entry[2] = True
        self.or_depth -= words.count("}")
        if self.or_depth <= 0:
            self.or_left, self.or_depth = None, 0

    def flush(self) -> None:
        if self.word is not None:
            if not self.drop_next:
                self.words.append(self.word)
            self.drop_next, self.word = False, None

    def end_command(self, operator: str = "") -> None:
        self.flush()
        words, self.words = self.words, []
        if words[:1] in (["then"], ["do"]):
            self.condition = False
        lead = _after(words, LEADS)[:1]
        if lead and lead[0] in CONDITIONS:
            self.condition = True
        if self.or_left and words:
            self.or_right(words)
        steers = operator == "&&" or self.condition
        for entry in self.commands[self.cmd_start:]:
            entry[2] = entry[2] or steers
        head = [word for word in words if word not in KEYWORDS - LOOPS - {"done"}][:1]
        if head and head[0] in LOOPS:
            self.loops.append(words[words.index(head[0]):][:2] == ["for", "attempt"])
        elif head == ["done"]:
            if not self.loops:
                raise Unparseable("done without a loop")
            self.loops.pop()
        if words:
            self.commands.append([words, self.in_attempt or any(self.loops), steers])
        if operator == "||":
            self.or_left = (self.cmd_start, len(self.commands))
        self.cmd_start = len(self.commands)


def _after(words: list[str], skip: set[str]) -> list[str]:
    """Words after any leading members of `skip`."""
    index = 0
    while index < len(words) and words[index] in skip:
        index += 1
    return words[index:]


def _stdin_shell(words: list[str]) -> bool:
    """Whether the command is bash/sh reading its script from stdin."""
    words = _strip(words)
    if not words or words[0].rsplit("/", 1)[-1] not in SHELLS:
        return False
    index, from_stdin = 1, False
    while index < len(words) and words[index][:1] in ("-", "+") and words[index] != "--":
        if SCRIPT_FLAG.fullmatch(words[index]):
            return False
        from_stdin = from_stdin or (words[index][1:2] != "-" and "s" in words[index][1:].rstrip("oO"))
        short = words[index][1:2] != "-"
        index += 2 if words[index] in SHELL_VALUE_OPTIONS or (short and words[index][-1] in "oO") else 1
    index += words[index:index + 1] == ["--"]
    return from_stdin or index >= len(words)  # an operand before -s is a script


def _strip(words: list[str], prefixed: bool = False) -> list[str]:
    """The command proper: keywords, assignments and (not `prefixed`) prefix commands removed."""
    while words:
        head = words[0]
        if head == "command" and words[1:2] in (["-v"], ["-V"]):
            return []
        if head in KEYWORDS or ASSIGNMENT.match(head):
            words = words[1:]
        elif head in PREFIXES and not prefixed:
            index = 1
            while index < len(words) and (words[index].startswith("-") or ASSIGNMENT.match(words[index])):
                index += 2 if words[index] in PREFIXES[head] else 1
            words = words[index + (head == "timeout"):]
            words = words[words[:1] == ["--"]:]  # after timeout's duration too
        elif head in ("bash", "sh") and len(words) > 1 and words[1].endswith("retry.sh"):
            words = words[1:]
        else:
            return words
    return words


def _gh(args: list[str]) -> str | None:
    """'read', 'write', 'local' (no API call), or None when the gh command is unclassified."""
    verb, action = (args + ["", ""])[:2]
    if verb in GH_LOCAL:
        return "local"
    if verb == "search":
        return "read"
    if verb != "api":
        return ("read" if action in GH_READS.get(verb, ()) else
                "write" if action in GH_WRITES.get(verb, ()) else None)
    method, fields, positional, index = None, False, [], 1
    while index < len(args):
        arg = args[index]
        if arg in ("-X", "--method"):
            method, index = args[index + 1:index + 2] or [""], index + 1
        elif arg.startswith(("-X", "--method=")):
            method = [arg.removeprefix("--method=").removeprefix("-X")]
        elif arg.startswith(GH_API_FIELDS):
            fields = True
            index += arg in GH_API_FIELDS
        elif arg in GH_API_VALUE_OPTIONS:
            index += 1
        elif not arg.startswith("-"):
            positional.append(arg)
        index += 1
    if method is not None:
        verb = method[0].upper()
        return None if verb not in HTTP_METHODS else "read" if verb in ("GET", "HEAD") else "write"
    if positional[:1] == ["graphql"]:
        return "write" if any("mutation" in arg for arg in args) else "read"
    return "write" if fields else "read"


def _fetch_kind(words: list[str]) -> str | None:
    """The retried fetch the words read as, if any; curl and wget read as any other name."""
    name, args, holds = words[0].rsplit("/", 1)[-1], words[1:], NPX_HOLDS
    if name == "git":
        index = 0
        while index < len(args) and args[index].startswith("-"):
            index += 2 if args[index] in GIT_VALUE_OPTIONS else 1
        return "git " + args[index] if args[index:index + 1] and args[index] in GIT_REMOTE_CALLS else None
    if name == "gh":
        kind = _gh(args)
        return "gh read" if kind == "read" else None if kind else "gh unclassified"
    if name == "npm":
        at = next((i for i, arg in enumerate(args) if not arg.startswith("-")), len(args))
        verb = args[at] if at < len(args) else ""
        if verb not in ("exec", "x"):
            return f"npm {verb}" if verb and verb not in NPM_LOCAL else None
        name, args, holds = "npx", args[:at] + args[at + 1:], NPX_HOLDS - {"--no-install"}
    lead = next((i for i, arg in enumerate(args) if arg.startswith("-m") or not arg.startswith("-")), len(args))
    option = args[lead] if lead < len(args) else ""
    module = (args[lead + 1] if option == "-m" and lead + 1 < len(args)
              else option[2:] if option.startswith("-m") and option != "-m" else name)
    if name == "uv" and args[:1] == ["pip"]:
        module = "pip"
    if name == "npx":
        # npm-exec(1) installs what it runs if missing, unless the last option before it holds.
        at = next((i for i, arg in enumerate(args) if not arg.startswith("-")), len(args))
        runs = at < len(args) or any(arg.startswith(("--package", "-p")) for arg in args)
        if runs and "".join(args[at - 1:at]) not in holds:
            return "npm exec"
        module = args[at] if at < len(args) else ""
    if re.fullmatch(r"pip[0-9.]*", module):
        verb = next((arg for arg in args if arg in ("install", "download", "wheel")), None)
        return f"pip {verb}" if verb else None
    return "playwright install" if module == "playwright" and "install" in args else None


def _curl_unbounded(args: list[str]) -> bool:
    pairs = list(zip(args, args[1:] + [""]))
    return not (any(flag == "--retry" and re.fullmatch("[1-9]", value) for flag, value in pairs)
                and "--retry-all-errors" in args
                and any(flag in ("--max-time", "-m") and value for flag, value in pairs))


def _positive(value: str) -> bool:
    return value.isascii() and value.isdigit() and int(value) > 0


def _attempt_bounded(words: list[str], name: str) -> bool:
    marker = words.index("--") if "--" in words[:5] else len(words)
    grace, limit, command = ((words[:marker] + words[marker + 1:])[2:5] + [""] * 3)[:3]
    return (words[:2] == ["timeout", "-k"] and _positive(grace) and _positive(limit)
            and command.rsplit("/", 1)[-1] == name)


def _transport(word: str, nominal: bool) -> str:
    """How git reaches a word shaped like a remote (GIT URLS, git-fetch(1)): http, ssh, other or ""."""
    word = word.partition("=")[2] if word.startswith("-") else word
    scheme, url, _ = word.partition("://")
    if word.startswith("$"):
        return ""
    if url:
        return GIT_SCHEMES.get(scheme, "other")
    if "::" in word:
        return "other"
    host = word.split("/")[0]
    return "ssh" if ":" in host and (nominal or "@" in host) else ""


def _git_unbounded(args: list[str], ssh: bool) -> str:
    config, index = {}, 0
    while index < len(args) and args[index].startswith("-"):
        if args[index] == "-c":
            name, _, value = "".join(args[index + 1:index + 2]).partition("=")
            config[name.lower()] = value
        index += 2 if args[index] in GIT_VALUE_OPTIONS else 1
    low = all(_positive(config.get(name, "")) for name in LOW_SPEED)
    rest = args[index + 1:]
    nominal = next((at for at, word in enumerate(rest) if not word.startswith("-")), -1)
    read = [(kind, word) for at, word in enumerate(rest) if (kind := _transport(word, at == nominal))]
    kind, word = read[0] if read else ("name", "")
    for other, second in read:
        if other != kind:
            return (f"with words of two transports, `{word}` ({kind}) and `{second}` ({other}); "
                    "write an option's value as --option=value")
    return "" if {"http": low, "ssh": ssh, "name": ssh or low, "other": False}[kind] else GIT_BOUNDS[kind].format(word)


def _ssh_export(words: list[str]) -> bool | None:
    """None without a mention of the variable; else whether a bounded one is exported."""
    if not any(SSH_COMMAND in word for word in words):
        return None
    if len(words) != 2 or words[0] != "export" or not words[1].startswith(SSH_COMMAND + "="):
        return False
    try:
        commands = _commands(words[1].partition("=")[2])
    except Unparseable:
        return False
    ssh = commands[0][0] if len(commands) == 1 else []
    if ssh[:1] != ["ssh"] or set(ssh[1::2]) - {"-o"}:
        return False
    first: dict[str, str] = {}
    for option in ssh[2::2]:
        name, _, value = option.partition("=")
        first.setdefault(name.lower(), value)
    return (_positive(first.get("connecttimeout", "")) and _positive(first.get("serveraliveinterval", ""))
            and _positive(first.get("serveralivecountmax", "1")))


def _proper(words: list[str]) -> tuple[list[str], list[str], bool]:
    """(the attempt inside any helper, the command proper, whether a helper runs it)."""
    attempt, words, wrapped = _strip(words, prefixed=True), _strip(words), False
    while words and words[0].rsplit("/", 1)[-1] in HELPERS:
        rest = words[1:]
        if words[0] == "api_retry":  # api_retry LABEL -- COMMAND...
            rest = rest[rest.index("--") + 1:] if "--" in rest else rest[1:]
        attempt, words, wrapped = _strip(rest, prefixed=True), _strip(rest), True
    return attempt, words, wrapped


def _inner(words: list[str]) -> str | None:
    """The script a `bash`/`sh -c` command runs, else None."""
    flag = next((i for i, word in enumerate(words[1:-1], 1) if SCRIPT_FLAG.fullmatch(word)), None)
    return words[flag + 1] if flag and words[0].rsplit("/", 1)[-1] in SHELLS else None


def _fetches(script: str):
    """(words, `_fetch_kind` of the command proper) per command, inner shells included: what `_judge` reads, except for curl and wget."""
    for words, _ in _commands(script):
        proper = _proper(words)[1]
        inner = _inner(proper)
        if inner is None:
            yield words, _fetch_kind(proper) if proper else None
        else:
            yield from _fetches(inner)


def _kinds(script: str):
    return (kind for _, kind in _fetches(script))


def _judge(words: list[str], retried: bool, ssh: bool = False) -> tuple[int, list[str]]:
    """(fetches recognised, reasons the command breaks the retry contract)."""
    attempt, words, wrapped = _proper(words)
    name = words[0].rsplit("/", 1)[-1] if words else ""
    inner = _inner(words)
    if inner is not None:
        judged = list(_judged(inner, retried, ssh))
        return sum(count for _, count, _ in judged), [r for _, _, reasons in judged for r in reasons]
    if name == "wget":
        return 1, ["wget"]
    if name == "curl":
        return 1, ["curl without --retry N, --retry-all-errors and --max-time"] if _curl_unbounded(words[1:]) else []
    kind = _fetch_kind(words) if words else None
    if kind == "gh unclassified":
        return 0, ["gh command not classified as read or write"]
    # A push's attempt loop repeats only after master moved.
    retried = retried and kind != "git push"
    reasons = [f"{kind} without the retry helper"] if kind and not (wrapped or retried) else []
    if (name == "gh" and _gh(words[1:]) != "local" or kind and kind.startswith("npm ")) and not _attempt_bounded(attempt, name):
        reasons.append(f"{kind or 'gh write'} without `timeout -k N N {name}`, N above 0, per attempt")
    lacking = _git_unbounded(words[1:], ssh) if kind and kind.startswith("git ") else ""
    return (1 if kind else 0), reasons + [f"{kind} {lacking}"] * bool(lacking)


def _commands(script: str) -> list[tuple[list[str], bool]]:
    text = re.sub(r"\$\{\{.*?\}\}", "GHEXPR", script, flags=re.S).replace("\\\n", " ")
    lexer = _Lexer(text)
    lexer.run()
    return [(words, attempt and steers) for words, attempt, steers in lexer.commands]


def _judged(script: str, retried: bool = False, ssh: bool = False):
    """(words, fetches recognised, reasons) per command; `ssh`: a bounded export precedes."""
    for words, loop in _commands(script):
        export = _ssh_export(words)
        ssh = ssh or bool(export)
        count, reasons = _judge(words, loop or retried, ssh)
        yield words, count, reasons + ([f"{SSH_COMMAND} not exported as `ssh -o Name=value ...` with "
                                        "ConnectTimeout and ServerAliveInterval above 0"] * (export is False))


def _steps() -> list[tuple[Path, str, dict, str | None]]:
    """(path, job id or composite, step, inherited shell) for every step."""
    found = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        doc = load_workflow(path) or {}
        default = ((doc.get("defaults") or {}).get("run") or {}).get("shell")
        for job_id, job in (doc.get("jobs") or {}).items():
            shell = ((job.get("defaults") or {}).get("run") or {}).get("shell", default)
            found += [(path, job_id, step, shell) for step in job.get("steps") or []]
    for path in sorted(ACTIONS.glob("*/action.y*ml")):
        runs = (load_workflow(path) or {}).get("runs") or {}
        if runs.get("using") == "composite":
            found += [(path, "composite", step, None) for step in runs.get("steps") or []]
    return found


def test_every_workflow_fetch_retries_transient_failures():
    violations, fetches = [], 0
    for path, job, step, shell in _steps():
        if "run" not in step:
            continue
        label = step.get("name") or step.get("id") or "unnamed step: " + step["run"].strip().split("\n")[0]
        where = f"{path.relative_to(ROOT)}: {job}: {label}"
        if (step.get("shell") or shell or "bash").split()[0] not in ("bash", "sh"):
            violations.append(f"{where}: run under a non-bash shell")
            continue
        try:
            for words, count, reasons in _judged(step["run"]):
                fetches += count
                shown = " ".join(_after(words, KEYWORDS))
                violations += [f"{where}: {reason}: {shown}" for reason in reasons]
        except Unparseable as error:
            violations.append(f"{where}: unparseable run ({error})")
    assert fetches, "no network command was read; the run walk is broken"
    assert not violations, "\n".join(violations)
