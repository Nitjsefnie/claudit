"""The node runner tolerates one timeout, while real failures stay failures."""
from __future__ import annotations

import subprocess
from unittest.mock import Mock, call

import pytest

from tests import node_run


def test_one_timeout_retries_with_the_same_argv_input_bound_and_kwargs(monkeypatch):
    argv = ("node", "-e", "process.stdout.write('ok')")
    result = subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")
    run = Mock(side_effect=[subprocess.TimeoutExpired(argv, 120), result])
    monkeypatch.setattr(node_run.subprocess, "run", run)
    kwargs = {
        "capture_output": True, "check": True, "text": True,
        "encoding": "utf-8", "errors": "strict", "cwd": "work",
        "env": {"NODE_ENV": "test"},
    }

    assert node_run.run_node(argv, input="source", timeout=120, **kwargs) is result

    assert run.call_args_list == [
        call(argv, input="source", timeout=120, **kwargs),
        call(argv, input="source", timeout=120, **kwargs),
    ]


def test_two_timeouts_propagate_after_two_calls_at_the_default_bound(monkeypatch):
    argv = ("node",)
    first = subprocess.TimeoutExpired(argv, 60)
    second = subprocess.TimeoutExpired(argv, 60)
    run = Mock(side_effect=[first, second])
    monkeypatch.setattr(node_run.subprocess, "run", run)

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        node_run.run_node(argv)

    assert raised.value is second
    assert run.call_args_list == [
        call(argv, input=None, timeout=60.0),
        call(argv, input=None, timeout=60.0),
    ]


@pytest.mark.parametrize("failure", ["returncode", "called-process-error", "os-error"])
def test_non_timeout_failures_are_not_retried(monkeypatch, failure):
    argv = ("node", "--check", "-")
    result = subprocess.CompletedProcess(argv, 7, stdout=b"", stderr=b"bad script")
    error = (subprocess.CalledProcessError(7, argv)
             if failure == "called-process-error" else FileNotFoundError("node"))
    run = Mock(return_value=result, side_effect=None if failure == "returncode" else error)
    monkeypatch.setattr(node_run.subprocess, "run", run)
    check = failure == "called-process-error"

    if failure == "returncode":
        assert node_run.run_node(argv, input=b"source", check=check) is result
    else:
        with pytest.raises(type(error)) as raised:
            node_run.run_node(argv, input=b"source", check=check)
        assert raised.value is error

    run.assert_called_once_with(argv, input=b"source", timeout=60.0, check=check)
