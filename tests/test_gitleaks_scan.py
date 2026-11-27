"""gitleaks_scan.sh: config and ignore list come from the base branch; every file diffs as text."""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, '.github', 'scripts', 'gitleaks_scan.sh')

# A stand-in for the binary: records its argv, its config and the suppression
# surfaces the tree holds at the moment it runs.
STUB = '''#!/usr/bin/env bash
printf '%s\\n' "$@" > "$CLAUDIT_STUB_ARGS"
while [ $# -gt 0 ]; do
  if [ "$1" = --config ]; then shift; cp "$1" "$CLAUDIT_STUB_CONFIG"; break; fi
  shift
done
if [ -e .gitleaksignore ]; then cp .gitleaksignore "$CLAUDIT_STUB_IGNORE"
else rm -f -- "$CLAUDIT_STUB_IGNORE"; fi
attributes=$(git rev-parse --git-path info/attributes)
if [ -f "$attributes" ]; then cp "$attributes" "$CLAUDIT_STUB_ATTRIBUTES"
else : > "$CLAUDIT_STUB_ATTRIBUTES"; fi
exit "${CLAUDIT_STUB_EXIT:-0}"
'''

BASE_CONFIG = '[extend]\nuseDefault = true\n# base\n'
WEAKER = '[extend]\nuseDefault = false\n# head\n'
DEFAULT = '[extend]\nuseDefault = true\n'
# The fixture commits are throwaway; they carry no attribution worth keeping.
IDENTITY = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.com',
            'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.com'}


@unittest.skipIf(sys.platform == 'win32', 'the scan script needs POSIX Bash')
class Scan(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.origin = self.root / 'origin.git'
        self.work = self.root / 'work'
        self.temp = self.root / 'temp'
        self.args = self.root / 'args'
        self.config = self.root / 'config'
        self.ignore = self.root / 'ignore'
        self.attributes = self.root / 'attributes'
        self.temp.mkdir()
        self.git(self.root, 'init', '-q', '--bare', str(self.origin))
        self.git(self.root, 'init', '-q', str(self.work))
        self.git(self.work, 'remote', 'add', 'origin', self.origin.as_uri())
        (self.work / 'gitleaks').write_text(STUB)
        (self.work / 'gitleaks').chmod(0o755)

    def git(self, cwd, *arguments):
        return subprocess.run(['git', '-C', str(cwd), *arguments], check=True,
                              capture_output=True, text=True,
                              env={**os.environ, **IDENTITY}).stdout

    def publish(self, files):
        """Commit `files` in the work tree and push them as the base branch."""
        for name, text in files.items():
            path = self.work / name
            if text is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(text)
        self.git(self.work, 'add', '-A')
        self.git(self.work, 'commit', '-qm', 'change')
        self.git(self.work, 'push', '-q', 'origin', 'HEAD:refs/heads/main')

    def run_scan(self, base='main', stub_exit=None):
        env = {**os.environ, 'RUNNER_TEMP': str(self.temp),
               'CLAUDIT_STUB_ARGS': str(self.args),
               'CLAUDIT_STUB_CONFIG': str(self.config),
               'CLAUDIT_STUB_IGNORE': str(self.ignore),
               'CLAUDIT_STUB_ATTRIBUTES': str(self.attributes)}
        env.pop('CLAUDIT_STUB_EXIT', None)
        if stub_exit is not None:
            env['CLAUDIT_STUB_EXIT'] = str(stub_exit)
        return subprocess.run([SCRIPT, base], cwd=self.work, env=env, check=False,
                              capture_output=True, text=True)

    def seen(self):
        arguments = self.args.read_text().splitlines()
        return arguments, self.args, self.config.read_text()

    def test_the_base_branch_config_is_used_even_when_the_tree_carries_another(self):
        self.publish({'.gitleaks.toml': BASE_CONFIG, 'a.py': 'a = 1\n'})
        (self.work / '.gitleaks.toml').write_text(WEAKER)
        done = self.run_scan()
        self.assertEqual(done.returncode, 0, done.stderr)
        arguments, _, config = self.seen()
        self.assertEqual(config, BASE_CONFIG)
        self.assertIn('--config', arguments)
        walk = '--log-opts=--full-history --all --diff-filter=uxdb --diff-merges=first-parent'
        self.assertLessEqual({'--ignore-gitleaks-allow', walk}, set(arguments))
        chosen = pathlib.Path(arguments[arguments.index('--config') + 1])
        self.assertTrue(chosen.is_absolute(), chosen)
        self.assertFalse(chosen.resolve().is_relative_to(self.work.resolve()), chosen)

    def test_the_base_branch_ignore_file_replaces_the_trees(self):
        self.publish({'.gitleaks.toml': BASE_CONFIG, '.gitleaksignore': '# base-ignore\n'})
        (self.work / '.gitleaksignore').write_text('# head-ignore\n')
        self.assertEqual(self.run_scan().returncode, 0)
        self.assertEqual(self.ignore.read_text(), '# base-ignore\n')
        self.assertEqual((self.work / '.gitleaksignore').read_text(), '# base-ignore\n')

    def test_a_tree_ignore_file_is_removed_when_the_base_has_none(self):
        self.publish({'.gitleaks.toml': BASE_CONFIG, '.gitleaksignore': None})
        (self.work / '.gitleaksignore').write_text('# head-ignore\n')
        self.assertEqual(self.run_scan().returncode, 0)
        self.assertFalse(self.ignore.exists(), self.ignore)
        self.assertFalse((self.work / '.gitleaksignore').exists())

    def test_a_symlinked_tree_ignore_file_is_replaced_not_written_through(self):
        for base_ignore in ('# base-ignore\n', None):
            with self.subTest(base_ignore=base_ignore):
                self.publish({'.gitleaks.toml': BASE_CONFIG, '.gitleaksignore': base_ignore,
                              'sentinel': 'kept\n'})
                (self.work / '.gitleaksignore').unlink(missing_ok=True)
                (self.work / '.gitleaksignore').symlink_to('sentinel')
                self.args.unlink(missing_ok=True)
                self.assertEqual(self.run_scan().returncode, 0)
                self.assertEqual((self.work / 'sentinel').read_text(), 'kept\n')
                self.assertTrue(self.args.exists(), 'the scan never ran')
                self.assertFalse((self.work / '.gitleaksignore').is_symlink())

    def test_every_file_is_forced_to_diff_as_text(self):
        self.publish({'.gitleaks.toml': BASE_CONFIG, '.gitattributes': 'a.py -diff\n'})
        self.assertEqual(self.run_scan().returncode, 0)
        self.assertIn('* diff', self.attributes.read_text().splitlines())

    def test_an_absent_unusable_or_empty_base_config_falls_back_to_the_default(self):
        self.publish({'.gitleaks.toml': BASE_CONFIG, 'a.py': 'a = 1\n'})
        for base_files in ({'.gitleaks.toml': None}, {'.gitleaks.toml': WEAKER},
                           {'.gitleaks.toml': ''}):
            self.publish({'a.py': 'a = 1\n', **base_files})
            self.assertEqual(self.run_scan().returncode, 0)
            _, _, config = self.seen()
            self.assertEqual(config, DEFAULT)

    def test_an_unknown_base_ref_refuses_before_gitleaks_runs(self):
        self.publish({'a.py': 'a = 1\n'})
        done = self.run_scan(base='release')
        self.assertNotEqual(done.returncode, 0)
        self.assertFalse(self.args.exists())

    def test_the_scan_status_is_the_scripts_status(self):
        self.publish({'a.py': 'a = 1\n'})
        self.assertEqual(self.run_scan(stub_exit=1).returncode, 1)

    def test_a_missing_argument_is_a_usage_refusal(self):
        done = subprocess.run([SCRIPT], cwd=self.work, check=False,
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 2)
        self.assertIn('usage', done.stderr)
