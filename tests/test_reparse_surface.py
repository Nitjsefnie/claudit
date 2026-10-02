"""The parse-surface gate: the corpus must walk every path the pass can reach.

Issue #503: the reparse bench measures one pass over `fixtures/r2_mini`,
and that mirror held only Claude-layout transcripts, so a regression in any
Codex or Kimi parse path, the lane key layout or the lane role mapping
moved no number in either direction. The mirror now carries one wire per
lane format plus a lane sidecar, and `scripts/ci/reparse_surface.py` names
the reachable surface by AST and fails when the corpus leaves any of it
unexercised.

Nothing here pins a measured number — the counts are machine- and
corpus-dependent by nature. What is pinned is the gate's SHAPE: the walk's
roots and its module boundary, the deny-by-default allowlist, the
fail-closed refusals, and the maintainer's own claim, asserted directly —
the committed corpus exercises all four formats `sniff_format` recognizes.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCH_DIR = REPO_ROOT / "scripts" / "ci"


def _load(name: str):
    """Import a scripts/ci module by path.

    scripts/ci is not a package, so this is the same by-path dance the
    sibling CI-script tests use; the directory goes on sys.path first so
    the module's own importlib imports of its siblings resolve.
    """
    if str(BENCH_DIR) not in sys.path:
        sys.path.insert(0, str(BENCH_DIR))
    spec = importlib.util.spec_from_file_location(
        name, BENCH_DIR / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


walk = _load("reparse_ast")
surface = _load("reparse_surface")
bench = _load("reparse_bench")


# --- the walk ----------------------------------------------------------------

def test_the_walk_starts_at_the_three_callables_the_pass_is_made_of():
    # reparse_phases' partition names them; if the roots moved, the gate
    # would be measuring a different pass than the one it prices.
    assert set(walk.ROOTS) == {
        ("parse", "parse_file"),
        ("parse_lanes", "sniff_format"),
        ("agent_sidecar", "apply_agent_sidecar"),
    }
    reached = walk.reachable_surface()
    for root in walk.ROOTS:
        assert ".".join(root) in reached, f"the walk lost its root {root}"


def test_the_walk_reaches_every_lane_parser_and_the_lane_adapter():
    # The gap this issue is about: the Claude-only corpus could not have
    # exercised any of these, and the walk is what now insists it does.
    reached = walk.reachable_surface()
    for name in ("parse_codex.parse",
                 "parse_kimi.parse_kimi_code",
                 "parse_kimi.parse_legacy",
                 "parse_lanes.to_claudit",
                 "parse_lanes.lane_agent_type",
                 "key_layout.in_lane_tree"):
        assert name in reached, f"the walk never reaches {name}"


def test_the_walk_crosses_a_module_boundary_the_attribute_expression_names():
    # `LANE_PARSERS[fmt](...)` is a call into all three lane parsers that
    # no attribute expression spells out, so an edge that only followed
    # `mod.func` calls would silently drop the entire lane surface.
    reached = walk.reachable_surface()
    assert "parse_codex.parse" in reached
    assert "parse_kimi.parse_legacy" in reached


def test_the_walk_stops_at_the_module_boundary():
    # The surface is one reviewable list, not the whole backend: a helper
    # the pass calls but that is not a parse path stays out of the gate.
    # pricing is the deliberate exclusion (see the module's docstring).
    reached = walk.reachable_surface()
    modules = set(reached.values())
    assert "pricing" not in modules
    assert modules <= set(walk.PARSE_SURFACE_MODULES)


def test_every_reached_function_exists_in_its_module():
    # A reachability claim about a name the module does not define is a
    # walk bug, and it would read as an unexercised function forever.
    reached = walk.reachable_surface()
    facts = walk._facts(REPO_ROOT)  # pylint: disable=protected-access
    for dotted in reached:
        module, _, qualname = dotted.partition('.')
        assert qualname in facts[module].functions, dotted


def _stub_tree(root: Path) -> Path:
    """A backend/ carrying every surface module as a one-function stub.

    Enough for the walk to read: the refusals below are about the tree
    being incomplete, not about what a stub parses.
    """
    backend = root / "backend"
    backend.mkdir()
    for name in walk.PARSE_SURFACE_MODULES:
        (backend / f"{name}.py").write_text(
            "def _stub():\n    return 1\n", encoding='utf-8')
    return backend


def test_the_walk_refuses_when_a_root_is_gone(tmp_path):
    # Fail closed: a root that is not there means the walk cannot say what
    # the pass can reach, and a short list is a silent pass.
    backend = _stub_tree(tmp_path)
    (backend / "parse.py").write_text("def other():\n    return 1\n",
                                      encoding='utf-8')
    with pytest.raises(ValueError, match='no root'):
        walk.reachable_surface(tmp_path)


def test_the_walk_refuses_a_call_it_cannot_resolve(tmp_path):
    # A bare callee the module never binds is a name the walk knows
    # nothing about. Dropping it silently would make the surface an
    # under-approximation that reads exactly like a covered one.
    backend = _stub_tree(tmp_path)
    (backend / "parse.py").write_text(
        "def parse_file(file_key, blob):\n    return mystery(file_key)\n",
        encoding='utf-8')
    (backend / "parse_lanes.py").write_text(
        "def sniff_format(blob):\n    return 'claude'\n", encoding='utf-8')
    (backend / "agent_sidecar.py").write_text(
        "def apply_agent_sidecar(parsed, sidecar, key):\n    return parsed\n",
        encoding='utf-8')
    with pytest.raises(ValueError, match='unbound callee'):
        walk.reachable_surface(tmp_path)


def test_the_walk_refuses_a_call_of_a_calls_result(tmp_path):
    # `node.func` holds a Call as well as a Name: a factory's result called
    # back is a callee the walk would otherwise drop without noticing.
    backend = _stub_tree(tmp_path)
    (backend / "parse.py").write_text(
        "def _factory():\n    return _stub\n\n"
        "def parse_file(file_key, blob):\n    return _factory()(file_key)\n",
        encoding='utf-8')
    with pytest.raises(ValueError, match='call of a call'):
        walk.reachable_surface(tmp_path)


def test_the_walk_refuses_a_missing_surface_module(tmp_path):
    backend = _stub_tree(tmp_path)
    (backend / "parse_lanes.py").unlink()
    with pytest.raises(ValueError, match='missing'):
        walk.reachable_surface(tmp_path)


def test_a_class_reached_from_another_module_expands_into_its_methods(tmp_path):
    # The class-expansion must read the methods off the callee's OWN
    # module. Reading them off the module whose function made the call
    # expands it to nothing, and a same-module class cannot tell the two
    # readings apart — which is why this builds one in a DIFFERENT module,
    # reached only from `parse`.
    backend = _stub_tree(tmp_path)
    (backend / "parse.py").write_text(
        "from backend.key_layout import InLaneTree\n\n"
        "def parse_file(file_key, blob):\n"
        "    return InLaneTree().helper()\n", encoding='utf-8')
    (backend / "parse_lanes.py").write_text(
        "def sniff_format(blob):\n    return 'claude'\n", encoding='utf-8')
    (backend / "agent_sidecar.py").write_text(
        "def apply_agent_sidecar(parsed, sidecar, key):\n    return parsed\n",
        encoding='utf-8')
    (backend / "key_layout.py").write_text(
        "class InLaneTree:\n"
        "    def __init__(self):\n        self.x = 1\n"
        "    def helper(self):\n        return self.other()\n"
        "    def other(self):\n        return 2\n"
        "    def never_called(self):\n        return 3\n",
        encoding='utf-8')
    reached = walk.reachable_surface(tmp_path)
    assert "key_layout.InLaneTree" not in reached, (
        "a class has no code object and could never be exercised")
    for method in ("helper", "other", "never_called"):
        assert f"key_layout.InLaneTree.{method}" in reached, (
            f"a reached class must reach its own method {method}, including "
            "one only reachable through self")


def test_a_class_is_reached_in_all_its_methods_but_is_not_itself_a_finding():
    # `_LineWalk` is instantiated by the pass, so every method is a path
    # the corpus has to have walked; the class itself has no code object
    # and could never be exercised.
    reached = walk.reachable_surface()
    assert "parse._LineWalk" not in reached
    assert "parse._LineWalk.handle_assistant_line" in reached
    assert "parse._LineWalk._record_tool_uses" in reached


def test_a_lookup_of_a_container_held_on_a_module_resolves(tmp_path):
    # `mod.TABLE[fmt](...)` is the same grammar as the bare-name form the
    # walk already reads; a Subscript arm that only accepts a Name base
    # drops it silently.
    backend = _stub_tree(tmp_path)
    (backend / "parse_lanes.py").write_text(
        "def sniff_format(blob):\n    return 'claude'\n", encoding='utf-8')
    (backend / "agent_sidecar.py").write_text(
        "def apply_agent_sidecar(parsed, sidecar, key):\n    return parsed\n",
        encoding='utf-8')
    (backend / "key_layout.py").write_text(
        "def classify(key):\n    return 1\n\n"
        "def never_called(key):\n    return 2\n\n"
        "TABLE = {'a': classify}\n", encoding='utf-8')
    (backend / "parse.py").write_text(
        "from backend import key_layout\n\n"
        "def parse_file(file_key, blob):\n"
        "    return key_layout.TABLE['a'](file_key)\n", encoding='utf-8')
    reached = walk.reachable_surface(tmp_path)
    assert "key_layout.classify" in reached, (
        "a module-attribute lookup of a container must resolve to the "
        "functions the container holds")


def test_a_container_built_inside_a_function_is_read(tmp_path):
    # Over-approximating on purpose: two functions may each build a table
    # under one name, and adding both is the safe direction for a coverage
    # gate. Dropping one is not.
    backend = _stub_tree(tmp_path)
    (backend / "parse_lanes.py").write_text(
        "def sniff_format(blob):\n    return 'claude'\n", encoding='utf-8')
    (backend / "agent_sidecar.py").write_text(
        "def apply_agent_sidecar(parsed, sidecar, key):\n    return parsed\n",
        encoding='utf-8')
    (backend / "key_layout.py").write_text(
        "def hidden(key):\n    return 1\n\n"
        "def build(fmt):\n"
        "    TABLES = {'a': hidden}\n"
        "    return TABLES[fmt](1)\n", encoding='utf-8')
    (backend / "parse.py").write_text(
        "from backend import key_layout\n\n"
        "def parse_file(file_key, blob):\n"
        "    return key_layout.build('a')\n", encoding='utf-8')
    assert "key_layout.hidden" in walk.reachable_surface(tmp_path)


def test_the_walk_refuses_a_call_through_a_parameter(tmp_path):
    # `callback(payload)`: the target is whatever the caller passed. A
    # same-named binding elsewhere in the module would otherwise let it
    # read as resolved, and the call would be dropped in silence.
    backend = _stub_tree(tmp_path)
    (backend / "parse_lanes.py").write_text(
        "def sniff_format(blob):\n    return 'claude'\n", encoding='utf-8')
    (backend / "agent_sidecar.py").write_text(
        "def apply_agent_sidecar(parsed, sidecar, key):\n    return parsed\n",
        encoding='utf-8')
    (backend / "parse.py").write_text(
        "def parse_file(file_key, blob):\n"
        "    return _run(handler)\n\n"
        "def _run(handler):\n    return handler(1)\n", encoding='utf-8')
    with pytest.raises(ValueError, match='callee is a parameter'):
        walk.reachable_surface(tmp_path)


def test_the_walk_refuses_a_lookup_it_cannot_name(tmp_path):
    # `f()['k']()` and friends: a subscript whose base is neither a Name
    # nor an Attribute is a callee the walk knows nothing about.
    backend = _stub_tree(tmp_path)
    (backend / "parse.py").write_text(
        "def _make():\n    return {}\n\n"
        "def parse_file(file_key, blob):\n"
        "    return _make()['k'](file_key)\n", encoding='utf-8')
    with pytest.raises(ValueError, match='subscript of'):
        walk.reachable_surface(tmp_path)


def test_a_nested_def_is_not_a_finding_of_its_own():
    # A nested helper runs or does not with the function it is written
    # inside, so listing it would be a finding no corpus can satisfy.
    tree = ast.parse("def outer():\n    def inner():\n        return 1\n"
                     "    return inner\n")
    assert walk._defines(tree) == {"outer"}  # pylint: disable=protected-access


# --- the executed side -------------------------------------------------------

def test_a_decorated_function_is_found_by_its_code_object():
    # functools leaves a wrapper with no `__code__` of its own, so the
    # table would miss it and the gate would report a path the corpus
    # demonstrably walks.
    from backend import bash_churn  # pylint: disable=import-outside-toplevel

    # pylint: disable=protected-access
    code = walk._code_of(bash_churn.BashCommand.parts)
    assert code is bash_churn.BashCommand.parts.func.__code__
    assert (walk._code_of(bash_churn.BashCommand.churn)
            is bash_churn.BashCommand.churn.__code__)
    assert code is not None


def test_the_code_object_table_covers_the_whole_reachable_surface():
    reached = walk.reachable_surface()
    assert set(walk.code_objects(reached).values()) == set(reached)


# --- the allowlist -----------------------------------------------------------

def test_an_allowlist_entry_without_a_reason_is_refused(tmp_path):
    # Deny by default: an excuse with no stated reason is the hole the
    # file exists to close, and it must not load.
    path = tmp_path / "allowlist.json"
    path.write_text(json.dumps({"parse.parse_file": "   "}), encoding='utf-8')
    with pytest.raises(ValueError, match='carries no reason'):
        surface.allowlist_entries(path)


def test_a_missing_allowlist_is_refused(tmp_path):
    with pytest.raises(ValueError, match='missing'):
        surface.allowlist_entries(tmp_path / "absent.json")


def test_an_allowlisted_name_excuses_only_itself():
    # The cheaper reading a future refactor reaches for is a prefix or a
    # substring match: `name.startswith(entry)` would excuse
    # `bash_reads.scan_command` and `bash_reads.scan_path` for nothing.
    # The entry names ONE function, and its near misses stay reported.
    reached = {"bash_reads.scan": "bash_reads",
               "bash_reads.scan_command": "bash_reads"}
    allowed = {"bash_reads.scan": "one dead branch"}
    assert surface.unexercised(reached, set(reached), allowed) == []
    # The corpus walked the allowlisted one and not its sibling: under a
    # prefix or substring match the sibling would be excused for nothing,
    # and this is the assertion that dies.
    assert surface.unexercised(reached, {'bash_reads.scan'}, allowed) == [
        'bash_reads.scan_command']


def test_an_allowlist_entry_for_a_function_that_is_gone_is_reported():
    # A deleted or renamed function must delete its excuse in the same
    # change: an entry nothing reads is a rule that outlived its subject.
    stale = surface.stale_allowlist(
        {"parse.parse_file": "parse"}, {"parse.gone_away": "deleted"})
    assert stale == ["parse.gone_away"]


def test_the_committed_allowlist_excuses_nothing_the_corpus_could_walk():
    # Every entry has to be a function the walk still reaches, or the gate
    # would be carrying an excuse for a function it never checks.
    allowed = surface.allowlist_entries()
    reached = walk.reachable_surface()
    assert surface.stale_allowlist(reached, allowed) == []
    # #505 deleted the gate's one excuse with the dead branch it excused
    # (parse._tool_access's Bash arm, which its only caller can never take):
    # the gate runs at full strictness, and an entry may return only with a
    # reachable function no fixture can walk.
    assert allowed == {}


# --- the corpus itself -------------------------------------------------------

def _sniff(blob: bytes) -> str:
    # pylint: disable-next=import-outside-toplevel
    from backend.parse_lanes import sniff_format
    return sniff_format(blob)


def test_the_committed_corpus_exercises_every_format_the_sniff_names():
    # The maintainer's words, asserted directly: the fixtures exercise all
    # reparse paths, and sniff_format recognizes these four.
    formats = {_sniff(entry.blob) for entry in bench.corpus()}
    assert formats == {"claude", "codex", "kimi-code", "legacy"}


def _corpus():
    return surface.corpus(bench.corpus(), bench.Entry)


def test_the_surface_corpus_keeps_the_mirror_and_adds_the_samples():
    # Not a re-glob of `corpus()`'s own two directories — that cannot
    # fail. What can: the union DROPS a mirror entry, repeats a key, or
    # stops being larger than the mirror (which is what makes the samples
    # worth walking at all).
    mirror = [entry.key for entry in bench.corpus()]
    keys = [entry.key for entry in _corpus()]
    assert len(keys) == len(set(keys)), "a corpus key is listed twice"
    assert set(mirror) <= set(keys), "a mirror transcript was dropped"
    samples = set(keys) - set(mirror)
    assert len(samples) > len(mirror), (
        "the samples no longer add to the corpus: the union has collapsed "
        "onto the mirror, so the gate is measuring only what the bench "
        "already measures")
    assert "parser/kimi_code_min.jsonl" in samples, (
        "a known lane sample is absent from the corpus")


def test_the_gate_refuses_when_the_mirror_loses_a_format():
    # The union answers "do the committed fixtures walk every parse path?".
    # This is the narrower question the bench's NUMBER depends on, and it
    # has to be a claim of the gate's own: the samples carry all four
    # formats, so a mirror that loses one leaves the union green.
    mirror = bench.corpus()
    assert surface.mirror_gap(mirror) == [], surface.mirror_gap(mirror)
    claude_only = [entry for entry in mirror
                   if "/sessions/" not in entry.key]
    assert surface.mirror_gap(claude_only) == [
        "codex", "kimi-code", "legacy"]


def test_a_report_naming_an_absent_format_never_reads_as_a_pass():
    text = surface.report([], '', ["codex", "legacy"])
    assert "no longer carries" in text
    assert "every reachable function was exercised" not in text


def test_the_surface_corpus_reads_nothing_but_committed_bytes():
    for entry in _corpus():
        assert entry.blob, f"{entry.key} read no bytes"


# --- the gate ----------------------------------------------------------------

def _run_gate() -> subprocess.CompletedProcess:
    """The gate as a PROCESS, which is how it always runs (issue #503).

    A fresh interpreter is not ceremony: several surface functions sit
    behind an ``lru_cache``, so a process that had already parsed the
    corpus once would see the cached path and report a function the corpus
    does in fact walk as unexercised.
    """
    return subprocess.run(  # pylint: disable=subprocess-run-check
        [sys.executable, str(BENCH_DIR / "reparse_surface.py")],
        capture_output=True, text=True, timeout=300, check=False)


def test_the_gate_passes_on_the_committed_corpus():
    done = _run_gate()
    assert done.returncode == 0, done.stderr
    assert 'every reachable function was exercised' in done.stderr


def test_the_gate_fails_when_a_reachable_path_is_not_walked():
    # Would this fail if the fix regressed? Yes: drop one executed
    # function from the set the gate diffs and the gate must name it.
    reached = walk.reachable_surface()
    allowed = surface.allowlist_entries()
    dropped = 'parse.parse_file'
    executed = {name for name in reached if name != dropped}
    assert surface.unexercised(reached, executed, allowed) == [dropped]


def test_the_gate_is_fail_closed_on_an_unmeasurable_surface(tmp_path):
    missing, note = surface.gap([], bench.run_pass, root=tmp_path)
    assert missing is None
    assert 'missing' in note


def test_the_report_names_an_unexercised_function_and_the_two_way_out():
    text = surface.report(['parse.parse_file'], '')
    assert 'parse.parse_file' in text
    assert 'reparse_surface_allowlist.json' in text
    assert 'every reachable function was exercised' in surface.report([])


def test_a_not_measured_report_never_reads_as_a_pass():
    text = surface.report(None, 'this interpreter has no sys.monitoring')
    assert 'NOT MEASURED' in text
    assert 'every reachable function was exercised' not in text


@pytest.mark.skipif(not hasattr(sys, 'monitoring'),
                    reason='the surface gate needs sys.monitoring')
def test_the_bench_gates_on_the_surface_beside_its_measurement():
    # The measured number and the surface are one verdict, so the bench's
    # own run carries the gate — as a child, so the caches above cannot
    # decide the answer.
    assert bench.SURFACE_GATE.exists()
    assert bench.check_surface() == 0
