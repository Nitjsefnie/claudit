"""The narrow derivation's index pins (issue #715).

The narrow tree path (`_derive_narrow`) walks server-text modules
first and resolves further def-sites through the vocabulary index, so
the registry derivation stops paying the walk for every fixture module
in the tree. These pins prove the narrowed path equals the full sweep
on the shapes the index has to carry: a chain through a non-server
module, and a nested fixture def the module level's own vocabulary
cannot see.
"""
from __future__ import annotations


from tests.test_db_marker import (_derive_from_modules, _derive_narrow,
                                  _modules)

# The server token, split: this file's own text must never carry a
# server token whole (the same trick the parity file's seeds use), or
# the marking guard would see this file as rooted.
# pylint: disable-next=implicit-str-concat
_SERVER = 'viz_' 'conn'


def test_a_chain_through_a_non_server_module_is_resolved(tmp_path):
    (tmp_path / "test_a_root.py").write_text(
        "import pytest\n\n\n@pytest.fixture\n"
        f"def grown():\n    return {_SERVER}()\n", encoding="utf-8")
    (tmp_path / "test_b_chain.py").write_text(
        "import pytest\n\n\n@pytest.fixture\n"
        "def chained(grown):\n    return None\n", encoding="utf-8")
    planted = list(_modules(tmp_path))
    assert _derive_narrow(tmp_path) == _derive_from_modules(planted)
    assert _derive_narrow(tmp_path) == {"grown", "chained"}


def test_a_nested_fixture_def_is_resolved_through_the_index(tmp_path):
    # The module level's own co_names cannot see a nested def; the
    # index admits on the all-level union (co_varnames and code-object
    # names included), so this shape must resolve like any other.
    (tmp_path / "test_a_root.py").write_text(
        "import pytest\n\n\n@pytest.fixture\n"
        f"def grown():\n    return {_SERVER}()\n", encoding="utf-8")
    (tmp_path / "test_b_nested.py").write_text(
        "import pytest\n\n\n"
        "def outer():\n"
        "    @pytest.fixture\n"
        "    def nested(grown):\n        return None\n"
        "    return None\n", encoding="utf-8")
    planted = list(_modules(tmp_path))
    assert _derive_narrow(tmp_path) == _derive_from_modules(planted)
    assert _derive_narrow(tmp_path) == {"grown", "nested"}
