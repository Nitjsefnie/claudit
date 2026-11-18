"""is_noop marks a shell command that does nothing, and only such a command (issue #1366)."""
import pytest

from backend.noop_command import is_noop


@pytest.mark.parametrize('command', [
    'true', ':', 'echo ok', 'echo noop', 'echo "waiting"', "echo 'done'", 'printf ready',
    'echo -n x', 'echo a; true', 'echo a && echo b', '  echo ok\n', 'true || :'])
def test_noop(command):
    assert is_noop(command)


@pytest.mark.parametrize('command', [
    '', 'ls', 'echo $?', 'echo "$HOME"', 'echo `date`', 'echo x > f', 'echo x >> f',
    'ls; echo done', 'echo ok && rm f', 'sleep 5', 'false', 'echo a | tee f', 'echo \\$x',
    'cat <<EOF'])
def test_not_noop(command):
    assert not is_noop(command)
