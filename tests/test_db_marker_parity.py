"""Parity between the derivation and its full-sweep control.

The control pair that keeps the incremental derivation honest: both
forms must root exactly the same fixture sets on the same modules. The
pair and its seeds moved here from tests/test_db_marker.py (issue
#715) so the guard module stays under the test-file size cap; nothing
about either form changed. `seeded_root`'s split spelling below is the
same trick the marking guard's pins use: this file's own text must
never carry a server token whole, or the marking guard would see this
file as rooted.
"""
from __future__ import annotations

import ast

from tests.test_db_marker import (_arg_names, _derive_from_modules,
                                  _functions, _mention_roots, _modules,
                                  _registered_fixtures,
                                  _rooted_fixture_names)


def _derive_by_sweep(modules) -> set[str]:
    """The derivation exactly as it read before the incremental union.

    The control the optimisation is measured against: it asks
    `_rooted_fixture_names` for a full sweep of every module's fixture
    map at every call, which is what `_derive_from_modules` used to do.
    """
    funcs = {name: _functions(tree) for name, tree, _ in modules}
    fixtures = _registered_fixtures(modules)
    rooted = {name: _mention_roots(funcs[name], source)
              for name, _, source in modules}
    changed = True
    while changed:
        changed = False
        for mod, fns in funcs.items():
            for fname, fn in fns.items():
                if fname in rooted[mod]:
                    continue
                calls = {n.func.id for n in ast.walk(fn)
                         if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Name)}
                rooted_params = (_arg_names(fn)
                                 & _rooted_fixture_names(fixtures, rooted))
                if calls & rooted[mod] or rooted_params:
                    rooted[mod].add(fname)
                    changed = True
    return _rooted_fixture_names(fixtures, rooted)


def _seeded_modules() -> tuple:
    """Three modules chained by parameter name, as (name, AST, source).

    `seeded_root` mentions a server call, so it is a root from the first
    round. `late_root` roots only once `seeded_root` is a rooted NAME --
    the step where the union gains something the initial sweep could not
    carry. `consumer` then roots through `late_root`, and it is
    REGISTERED, so a union that dropped the delta would leave it
    unrooted: the seeded violation this parity pair has to catch.
    """
    # The server token is spelled in two halves ON PURPOSE: the seeded
    # AST still contains it, so `seeded_root` is still a mention root
    # and the chain still starts here, while this file's own text never
    # contains the token and the marking guard below sees no server call
    # to flag. Nothing here reaches a server; the string is only parsed.
    # pylint: disable-next=implicit-str-concat
    server = 'viz_' 'conn'
    sources = {
        "seeded": 'import pytest\n\n\n@pytest.fixture\n'
                  f'def seeded_root():\n    return {server}()\n',
        "late": 'import pytest\n\n\n@pytest.fixture\n'
                'def late_root(seeded_root):\n    return None\n',
        "consumer": 'import pytest\n\n\n@pytest.fixture\n'
                    'def consumer(late_root):\n    return None\n',
    }
    return tuple((name, ast.parse(src), src) for name, src in sources.items())


# The frozen parity slice: four real, fixture-carrying modules, named.
# Named rather than the dynamic biggest-four so this test's cost
# tracks these files alone, never tree membership (issue #675).
# test_ci_reseed replaced test_workflow_release when the gate's fixture
# half moved to compile-level vocabulary (issue #679): the release
# module's only ``fixture`` occurrences are comment prose, so the gate
# rightly skips it, and a parity slice must name modules the gate
# admits. All four carry real fixture decorators and fixture chains.
_PARITY_SLICE = ("test_web_metrics_rollup", "test_parse_lanes",
                 "test_ci_reseed", "test_api")


def test_the_incremental_derivation_equals_the_full_sweep_on_real_source():
    # Parity on REAL sources, both forms over the same slice: a union
    # that dropped or double-counted a name would move a fixture in or
    # out of the registry this module guards. The slice is a FROZEN
    # list of four real modules, named: a dynamic biggest-N would
    # re-price this test whenever a pull request's added lines pushed
    # a different file into the top four (issue #675), and a guard that
    # costs more than the thing it guards is not a guard. The named
    # four were the largest modules when the slice froze. Their
    # fixture chains resolve from the initial mention-root sweep alone,
    # so this real-source leg on its own does not discriminate a broken
    # incremental union — the seeded pair below is the leg that does;
    # the two together are the control.
    by_name = {name: (name, tree, source)
               for name, tree, source in _modules()}
    missing = [name for name in _PARITY_SLICE if name not in by_name]
    assert not missing, (
        f"the frozen parity slice named missing modules: {missing}")
    biggest = [by_name[name] for name in _PARITY_SLICE]
    assert _derive_from_modules(biggest) == _derive_by_sweep(biggest)


def test_the_incremental_derivation_equals_the_full_sweep_on_seeded_modules():
    # The same pair on a seeded tree whose transitive chain runs THROUGH
    # the union's delta, and which ends in a registered fixture that must
    # be rooted. Both forms are the production code path: `_sweep` is the
    # pre-optimisation control, `_derive_from_modules` is what ships, so
    # breaking the union fails here rather than passing vacuously.
    modules = _seeded_modules()
    derived = _derive_from_modules(modules)
    assert derived == _derive_by_sweep(modules)
    assert derived == {"seeded_root", "late_root", "consumer"}
