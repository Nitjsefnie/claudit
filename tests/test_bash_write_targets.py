"""Named Bash file and directory writes (backend/bash_reads.py)."""
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


@pytest.mark.parametrize("family,expected", [
    ("ln", ["/work/node_modules"]),
    ("touch", ["/work/first.txt", "/work/second.txt"]),
    ("rsync", ["/work/mirror"]),
    ("tar_extract", ["/work/extracted"]),
    ("unzip", ["/work/extracted"]),
    ("curl_output", ["/work/downloaded"]),
    ("wget", ["/work/file.txt"]),
    ("curl_file_url", ["/work/data.txt"]),
    ("python_venv", ["/work/.venv"]),
])
def test_bash_family_write_targets(family, expected):
    out = parse.parse_file(
        "k/sess-write/sess-write.jsonl",
        (FIX / f"bash_{family}_write_targets.jsonl").read_bytes())
    assert out["tool_uses"][0]["write_targets"] == expected


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


@pytest.mark.parametrize("family,added", [
    ("curl_output", 1), ("wget", 1), ("tar_extract", 1), ("unzip", 1),
    ("rsync", 1), ("python_venv", 1), ("touch", 0), ("ln", 0),
])
def test_bash_family_churn_estimate(family, added):
    blob = (FIX / f"bash_{family}_write_targets.jsonl").read_bytes()
    tool = parse.parse_file("k/sess-write/sess-write.jsonl", blob)["tool_uses"][0]
    assert (tool["lines_added"], tool["lines_deleted"]) == (added, 0)


@pytest.mark.parametrize("command", [
    "curl -o out https://example.test/data.txt",
    "wget -O out https://example.test/data.txt",
    "tar -xf archive.tar -C out", "unzip archive.zip -d out",
    "rsync source out", "python3 -m venv out",
])
def test_bash_unknown_write_fallback_is_once_per_call(command):
    assert bash_churn(command + "; " + command) == (1, 0)
    assert bash_churn(command + r"; printf 'a\nb\n' > known.txt") == (2, 0)
    assert bash_churn("D=out; " + command.replace("out", "$D")) == (1, 0)


@pytest.mark.parametrize("family", ["curl_output", "wget", "tar_extract", "unzip", "rsync", "python_venv"])
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


@pytest.mark.parametrize("family", ["curl_output", "wget", "tar_extract", "unzip", "rsync", "python_venv"])
def test_bash_unknown_write_churn_survives_disabled_intake(family):
    blob = (FIX / f"bash_{family}_write_targets.jsonl").read_bytes()
    policy = CollectionPolicy(frozenset({"context_intake"}), COLLECTION_SPECS)
    tool = parse.parse_file(
        "k/sess-write/sess-write.jsonl", blob, collection=policy)["tool_uses"][0]
    assert tool["write_targets"] is None
    assert (tool["lines_added"], tool["lines_deleted"]) == (1, 0)


def test_unzip_comment_only_is_not_a_write():
    assert not scan("unzip -z archive.zip -d out", "/work")[2]
