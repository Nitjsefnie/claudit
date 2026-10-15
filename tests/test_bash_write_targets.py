"""Named Bash file and directory writes (backend/bash_reads.py)."""
from __future__ import annotations

from pathlib import Path

import pytest

from backend import parse
from backend.bash_reads import scan

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "parser"


@pytest.mark.parametrize("family,expected", [
    ("ln", ["/work/node_modules"]),
    ("touch", ["/work/first.txt", "/work/second.txt"]),
    ("rsync", ["/work/mirror"]),
    ("tar_extract", ["/work/extracted"]),
    ("unzip", ["/work/extracted"]),
    ("curl_output", ["/work/downloaded"]),
    ("wget", ["/work/file.txt"]),
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
