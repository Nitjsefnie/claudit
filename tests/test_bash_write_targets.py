from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend import parse
from backend.bash_reads import scan
from backend.bash_churn import bash_churn
from backend.flags import CollectionPolicy
from backend.flag_registry import COLLECTION_SPECS

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "parser"


@pytest.mark.parametrize("family,expected,added", [
    pytest.param("ln", ["/work/node_modules"], 0, id="ln"),
    pytest.param("touch", ["/work/first.txt", "/work/second.txt"], 0, id="touch"),
    pytest.param("rsync", ["/work/mirror"], 1, id="rsync"),
    pytest.param("tar_extract", ["/work/extracted"], 1, id="tar_extract"),
    pytest.param("unzip", ["/work/extracted"], 1, id="unzip"),
    pytest.param("curl_output", ["/work/downloaded"], 1, id="curl_output"),
    pytest.param("wget", ["/work/file.txt"], 1, id="wget"),
    pytest.param("curl_file_url", ["/work/data.txt"], None, id="curl_file_url"),
    pytest.param("python_venv", ["/work/.venv"], 1, id="python_venv"),
    pytest.param("git_worktree", ["/work/checkout"], 1, id="git_worktree"),
    pytest.param("git_checkout", ["/work/first.txt", "/work/second.txt"], 1, id="git_checkout"),
    pytest.param("git_restore", ["/work/first.txt"], 1, id="git_restore"),
    pytest.param("compiler", ["/work/program"], 1, id="compiler"),
    pytest.param("pdftotext", ["/work/out.txt"], 1, id="pdftotext"),
    pytest.param("initdb", ["/work/pgdata"], 1, id="initdb"),
    pytest.param("git_clone", ["/work/checkout"], 1, id="git_clone"),
    pytest.param("git_init", ["/work/checkout"], 1, id="git_init"),
    pytest.param("perl_inplace", ["/work/first.txt", "/work/second.txt"], 1, id="perl_inplace"),
    pytest.param("rustfmt", ["/work/first.rs", "/work/second.rs"], 1, id="rustfmt"),
])
def test_bash_family_write_targets(family, expected, added):
    out = parse.parse_file(
        "k/sess-write/sess-write.jsonl",
        (FIX / f"bash_{family}_write_targets.jsonl").read_bytes())
    tool = out["tool_uses"][0]
    assert tool["write_targets"] == expected
    if added is not None:  # curl_file_url has only a target expectation.
        assert (tool["lines_added"], tool["lines_deleted"]) == (added, 0)


@pytest.mark.parametrize("command,expected", [
    ("ln source link", ["/work/link"]),
    ("ln -sf source link", ["/work/link"]),
    ("ln -sfn source link", ["/work/link"]),
    ("ln a b out/", ["/work/out"]),
    ("ln -st out a b", ["/work/out"]),
    ("ln --target-directory=out a", ["/work/out"]),
    ("ln --target-directory out a", ["/work/out"]),
    ("ln -s source", []), ("ln -s", []), ("ln -t out", []),
    ("touch first second", ["/work/first", "/work/second"]),
    ("touch -d 'yesterday' -t 202601010000 -r reference --time=mtime file", ["/work/file"]),
    ("touch --date=yesterday --reference=source file", ["/work/file"]),
    ("touch -acm --no-create --no-dereference file", ["/work/file"]),
    ("touch -- -name", ["/work/-name"]), ("touch -r reference", []),
    ("rsync -avz src/ mirror/", ["/work/mirror"]),
    ("rsync -a --exclude .venv --exclude '.jsh-*' src tests out", ["/work/out"]),
    ("rsync -e 'ssh -p 22' host:source local", ["/work/local"]),
    ("rsync -an src/ mirror/", []), ("rsync --dry-run src/ mirror/", []),
    ("rsync -a src host:path", []), ("rsync -a src user@host:path", []),
    ("rsync -a src rsync://host/path", []), ("rsync source", []),
    ("tar -xzf archive.tgz -C out", ["/work/out"]),
    ("tar -xf archive.tar --directory=out", ["/work/out"]),
    ("tar --extract --file archive.tar --directory out", ["/work/out"]),
    ("tar --get -C out", ["/work/out"]),
    ("tar -x -C out", ["/work/out"]),
    ("tar -czf archive.tar -C src .", []),
    ("tar -tf archive.tar -C src", []), ("tar -rf archive.tar -C src file", []),
    ("tar -uf archive.tar -C src file", []), ("tar -xf archive.tar", []),
    ("tar -xOf archive.tar -C out", []), ("tar -xc -C out", []),
    ("unzip archive.zip -d out", ["/work/out"]),
    ("unzip -oq archive.zip file -d out", ["/work/out"]),
    ("unzip -l archive.zip -d out", []), ("unzip -v archive.zip -d out", []),
    ("unzip -t archive.zip -d out", []), ("unzip -p archive.zip -d out", []),
    ("unzip -Z archive.zip -d out", []), ("unzip -c archive.zip -d out", []),
    ("unzip archive.zip", []), ("zip archive.zip -d out", []),
    ("curl -o file https://example.test/data", ["/work/file"]),
    ("curl --output file https://example.test/data", ["/work/file"]),
    ("curl --output=file https://example.test/data", ["/work/file"]),
    ("curl -fsSLo file https://example.test/data", ["/work/file"]),
    ("curl -O 'https://example.test/files/data.txt?q=1#part'", ["/work/data.txt"]),
    ("curl --remote-name https://example.test/files/data.txt", ["/work/data.txt"]),
    ("curl -fsSLO https://example.test/files/data.txt", ["/work/data.txt"]),
    ("curl -o - https://example.test/data", []),
    ("curl --output - https://example.test/data", []),
    ("curl -O https://example.test/dir/", []),
    ("curl -O https://example.test", []),
    ("curl https://example.test/data", []),
    ("curl --output-dir out -O https://example.test/data", []),
    ("wget -O file https://example.test/data", ["/work/file"]),
    ("wget 'https://example.test/files/data.txt?q=1#part'", ["/work/data.txt"]),
    ("wget -O - https://example.test/data", []),
    ("wget https://example.test/dir/", []), ("wget https://example.test", []),
    ("python -m venv env", ["/work/env"]),
    ("python3 -m venv --system-site-packages env", ["/work/env"]),
    ("python3 -m venv --copies --clear env", ["/work/env"]),
    ("python3 -m venv", []), ("python3 -m other env", []),
])
def test_bash_named_write_targets(command, expected):
    assert scan(command, "/work")[2] == expected


@pytest.mark.parametrize("command", [
    "ln -s source {path}", "ln -t {path} source", "ln {path} link",
    "touch {path}", "rsync source {path}", "tar -xf archive -C {path}",
    "unzip archive -d {path}", "curl -o {path} https://example.test/file",
    "curl -O {path}", "wget -O {path} https://example.test/file",
    "wget {path}", "python3 -m venv {path}",
    "git worktree add {path} HEAD", "git checkout -- {path}",
    "git restore {path}", "gcc main.c -o {path}",
    "pdftotext in.pdf {path}", "initdb -D {path}",
    "git clone https://example.test/repo {path}", "git init {path}",
    "perl -pi -e 's/old/new/' {path}", "rustfmt {path}",
])
@pytest.mark.parametrize("path", ["$UNKNOWN", '"$UNKNOWN/path"', "$(unknown)", "`unknown`", "*.txt"])
def test_bash_runtime_write_targets_are_not_invented(command, path):
    assert not scan(command.format(path=path), "/work")[2]


@pytest.mark.parametrize("command,expected", [
    ("D=out; ln -s source $D/link", ["/work/out/link"]),
    ("F=file; touch $F", ["/work/file"]),
    ("D=out; rsync src/ $D", ["/work/out"]),
    ("D=out; tar -xf archive -C $D", ["/work/out"]),
    ("D=out; unzip archive -d $D", ["/work/out"]),
    ("F=file; curl -o $F https://example.test/data", ["/work/file"]),
    ("URL=https://example.test/data; curl -O $URL", ["/work/data"]),
    ("URL=https://example.test/data; wget $URL", ["/work/data"]),
    ("D=out; python -m venv $D", ["/work/out"]),
    ("cd project && touch file", ["/work/project/file"]),
    ("curl https://example.test/data > saved.txt", ["/work/saved.txt"]),
    ("curl https://example.test/data | tee saved.txt", ["/work/saved.txt"]),
    ("touch 'literal*.txt'", ["/work/literal*.txt"]),
])
def test_bash_write_targets_resolve_literal_assignments(command, expected):
    assert scan(command, "/work")[2] == expected


@pytest.mark.parametrize("command", [
    "curl -o out https://example.test/data.txt",
    "wget -O out https://example.test/data.txt",
    "tar -xf archive.tar -C out", "unzip archive.zip -d out",
    "rsync source out", "python3 -m venv out",
    "git worktree add out HEAD", "git checkout HEAD -- out", "git restore out",
    "gcc -o out main.c", "pdftotext in.pdf out", "initdb --pgdata=out",
    "git clone https://example.test/repo out", "git init out",
    "perl -0pi -e 's/old/new/' out", "rustfmt out",
])
def test_bash_unknown_write_fallback_is_once_per_call(command):
    assert bash_churn(command + "; " + command) == (1, 0)
    assert bash_churn(command + r"; printf 'a\nb\n' > known.txt") == (2, 0)
    assert bash_churn("D=out; " + command.replace(" out", " $D")) == (1, 0)


@pytest.mark.parametrize("family", [
    "curl_output", "wget", "tar_extract", "unzip", "rsync", "python_venv",
    "git_worktree", "git_checkout", "git_restore", "compiler", "pdftotext",
    "initdb", "git_clone", "git_init", "perl_inplace", "rustfmt",
])
@pytest.mark.parametrize("result", [
    "Exit code 1\ncommand failed",
    "The user doesn't want to proceed with this tool use",
    "PreToolUse hook error: denied",
])
def test_bash_unknown_write_failure_gets_no_fallback(family, result):
    blob = (FIX / f"bash_{family}_write_targets.jsonl").read_bytes()
    outcome = {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": result}]}}
    tool = parse.parse_file("k/sess-write/sess-write.jsonl", blob + json.dumps(outcome).encode() + b"\n")["tool_uses"][0]
    assert (tool["lines_added"], tool["lines_deleted"]) == (0, 0)


@pytest.mark.parametrize("family", [
    "curl_output", "wget", "tar_extract", "unzip", "rsync", "python_venv",
    "git_worktree", "git_checkout", "git_restore", "compiler", "pdftotext",
    "initdb", "git_clone", "git_init", "perl_inplace", "rustfmt",
])
def test_bash_unknown_write_churn_survives_disabled_intake(family):
    blob = (FIX / f"bash_{family}_write_targets.jsonl").read_bytes()
    policy = CollectionPolicy(frozenset({"context_intake"}), COLLECTION_SPECS)
    tool = parse.parse_file(
        "k/sess-write/sess-write.jsonl", blob, collection=policy)["tool_uses"][0]
    assert tool["write_targets"] is None
    assert (tool["lines_added"], tool["lines_deleted"]) == (1, 0)


def test_unzip_comment_only_is_not_a_write():
    assert not scan("unzip -z archive.zip -d out", "/work")[2]


@pytest.mark.parametrize("command,expected", [
    ("git worktree add -Btopic out HEAD", ["/work/out"]),
    ("git worktree add --prefix=3 -b topic out", ["/work/out"]),
    ("git worktree list", []), ("git worktree prune", []),
    ("git worktree remove out", []), ("git worktree lock out", []),
    ("git checkout -- first second", ["/work/first", "/work/second"]),
    ("git checkout HEAD -- .", ["/work"]),
    ("git checkout branch", []), ("git checkout -p HEAD -- file", []),
    ("git checkout --patch -- file", []),
    ("git checkout -b topic HEAD -- file", ["/work/file"]),
    ("git restore first second", ["/work/first", "/work/second"]),
    ("git restore -s HEAD --worktree -- file", ["/work/file"]),
    ("git restore --source=HEAD file", ["/work/file"]),
    ("git restore --staged file", []), ("git restore -S file", []),
    ("git restore -SW file", ["/work/file"]),
    ("git restore --pathspec-from-file paths.txt", []),
    ("git restore --pathspec-from-file=paths.txt --pathspec-file-nul file", ["/work/file"]),
    ("git restore -p file", []), ("git restore --patch file", []),
    ("gcc -o program main.c", ["/work/program"]),
    ("g++ -oprogram main.cpp", ["/work/program"]),
    ("cc -c -g -O2 -I include -DDEBUG=1 -o main.o main.c", ["/work/main.o"]),
    ("clang -Wall -Wextra -std=c11 main.c -o program", ["/work/program"]),
    ("clang++ -std=c++20 main.cpp -oprogram", ["/work/program"]),
    ("gcc -o -Wall main.c", ["/work/-Wall"]),
    ("gcc -o '-std=c11' main.c", ["/work/-std=c11"]),
    ("gcc --version", []), ("gcc -Q --help", []), ("gcc main.c", []),
    ("gcc --version -o program", []), ("gcc --help -o program", []),
    ("gcc -fsyntax-only -o program main.c", []),
    ("gcc -o /dev/null main.c", []), ("gcc -o - main.c", []),
    ("pdftotext in.pdf out.txt", ["/work/out.txt"]),
    ("pdftotext -f 1 -l 2 -opw secret -oupw secret in.pdf out.txt", ["/work/out.txt"]),
    ("pdftotext -layout -upw secret in.pdf out.txt", ["/work/out.txt"]),
    ("pdftotext -- in.pdf -layout", ["/work/-layout"]),
    ("pdftotext in.pdf", []), ("pdftotext in.pdf -", []),
    ("pdftotext in.pdf /dev/null", []), ("pdftotext -f 1 in.pdf", []),
    ("initdb -D data", ["/work/data"]),
    ("initdb --pgdata=data", ["/work/data"]),
    ("initdb data", ["/work/data"]),
    ("initdb -s -D data", []), ("initdb --show -D data", []),
    ("initdb -Upostgres -Atrust -E UTF8 --auth-local=trust --auth-host scram-sha-256 "
     "--locale C --locale-provider=libc data", ["/work/data"]),
    ("initdb -U postgres", []), ("initdb", []),
    ("git clone --depth 1 --branch main url out", ["/work/out"]),
    ("git clone --bare url out", ["/work/out"]),
    ("git clone https://example.test/repo.git", ["/work/repo"]),
    ("git init", ["/work"]), ("git init .", ["/work"]),
    ("git init -b main out", ["/work/out"]),
    ("git init --bare -Bmain out", ["/work/out"]),
    ("git init -b", []),
    ("perl -i -pe 's/old/new/' first second", ["/work/first", "/work/second"]),
    ("perl -i.bak -pe 's/old/new/' file", ["/work/file"]),
    ("perl -pi -e 's/old/new/' file", ["/work/file"]),
    ("perl -0pi -e 's/old/new/' file", ["/work/file"]),
    ("perl -ipe 's/old/new/' file", ["/work/file"]),
    ("perl -pi.bak -e 's/old/new/' file", ["/work/file"]),
    ("perl -i filter.pl first second", ["/work/first", "/work/second"]),
    ("perl -pi -e 's/old/new/'", []),
    ("perl -pi -ce 's/old/new/' file", []),
    ("perl -cpi -e 's/old/new/' file", []),
    ("perl -pe 's/old/new/' file", []),
    ("rustfmt first.rs second.rs", ["/work/first.rs", "/work/second.rs"]),
    ("rustfmt --config hard_tabs=true --edition 2021 --emit files first.rs", ["/work/first.rs"]),
    ("rustfmt --emit=files first.rs", ["/work/first.rs"]),
    ("rustfmt --check first.rs", []), ("rustfmt --check", []),
    ("rustfmt --emit stdout first.rs", []), ("rustfmt --edition 2021", []),
    ("rustfmt", []),
])
def test_bash_named_command_write_targets_and_churn(command, expected):
    assert scan(command, "/work")[2] == expected
    assert bash_churn(command) == ((1, 0) if expected else (0, 0))


@pytest.mark.parametrize("command", [
    "git worktree add {path} HEAD", "git checkout HEAD -- {path}", "git restore {path}",
    "gcc -o{path} main.c", "pdftotext in.pdf {path}", "initdb --pgdata={path}",
    "git clone url {path}", "git init {path}",
    "perl -0pi -e 's/old/new/' {path}", "rustfmt {path}",
])
def test_bash_write_targets_resolve_cwd_and_assignments(command):
    assert scan("D=out; cd project && " + command.format(path="$D"), "/work")[2] == ["/work/project/out"]
    assert scan(command.format(path="'literal*.txt'"), "/work")[2] == ["/work/literal*.txt"]


@pytest.mark.parametrize("command", [
    "git checkout -- first {path}", "git restore first {path}",
    "git clone {path} out", "perl -pi -e 's/old/new/' first {path}", "rustfmt first {path}",
])
@pytest.mark.parametrize("path", ["$UNKNOWN", '"$UNKNOWN/path"', "$(unknown)", "`unknown`", "*.txt"])
def test_bash_unresolved_operands_refuse_all_write_targets(command, path):
    assert not scan(command.format(path=path), "/work")[2]


@pytest.mark.parametrize("source", [
    "https://example.test/repo.git", "https://example.test/repo/",
    "git@example.test:project/repo.git", "repo",
])
def test_bash_clone_directory_derivation(source):
    command = f"cd project && git clone {source}"
    assert scan(command, "/work")[2] == ["/work/project/repo"]
    assert bash_churn(command) == (1, 0)
