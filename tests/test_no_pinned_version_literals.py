"""SV-TEST-DATA's guard: no test pins repository-managed data with a literal.

A test that patches ``constants.PARSER_VERSION`` / ``PRICING_VERSION`` /
``MARKER_READER_VERSION`` (or sets the parser-version environment
variable) with a string literal silently changes meaning when the
committed value reaches the literal — the patch turns into a no-op, the
test keeps passing until the data moves under it, and then the failure
lands on an unrelated surface (a refresh bot's working tree, say).

The same holds for the LIVE-ROW shapes: a test that reads the committed
pricing document (``pricing.PRICING_JSON``), that binds its path at
module level, or that hands a live model or host name to ``rate_for`` /
``resolve`` / ``compute_cost`` breaks the same way when the committed
rows move.

The detector below is a pure function over source text: ``detect()``
lists the flagged sites (an AST scan), ``check()`` adds the allowlist
matching. An inline ``# sv-test-data: allow`` comment on the flagged
statement's first line excuses a site; a marker on a line with no
flagged site of either family is marker rot and fails the guard, so
stale excuses cannot accumulate.
"""
from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import tokenize
from pathlib import Path

from backend import pricing
from tests import scan_gate, version_literal_guard
from tests.version_literal_guard import (_match_wanted, _match_wanted_scan,
                                         _names_wanted_row, RATE_CALL_NAMES,
                                         VERSION_NAMES, Site, detect)

REPO_ROOT = Path(__file__).resolve().parents[1]

MARKER = re.compile(r"#\s*sv-test-data:\s*allow\b")

# The live-row Site.shape values, and the problem text each carries.
_LIVE_ROW_SHAPES = ("pricing-json", "path-bind", "live-call")
_LIVE_ROW_TEXT = {
    "pricing-json":
        "reads the committed pricing document (pricing-json path bind); "
        "add '# sv-test-data: allow' with a reason, or load synthetic data",
    "path-bind":
        "binds the committed pricing document path (pricing-json path "
        "bind); add '# sv-test-data: allow' with a reason, or load "
        "synthetic data",
    "live-call":
        "names a live rate row (live model or host in a pricing call); "
        "add '# sv-test-data: allow' with a reason, or derive the name "
        "from the current tables",
}


def _marker_lines(source: str) -> set[int]:
    """Lines carrying a real ``# sv-test-data: allow`` COMMENT token.

    tokenize, not a line regex: marker text inside a string literal (a
    test's synthetic code sample, say) is not a comment.
    """
    found: set[int] = set()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT and MARKER.search(token.string):
            found.add(token.start[0])
    return found


def _scan_admits(source: str, *, wanted_models: frozenset[str] = frozenset(),
                 wanted_hosts: frozenset[str] = frozenset()) -> bool:
    """Whether one module's source can hold anything check() can flag.

    The fail-closed gate that keeps this file's tree scan (issue #510)
    from walking every unrelated module: every clause admits a strict
    superset of the shapes detect()/check() flag, so a skip only saves
    the walk, never hides a site. Admission is compile-level vocabulary
    (tests/scan_gate) — identifiers land verbatim in ``co_names``
    (splitting and escaping are impossible for identifiers) and string
    constants fold into ``co_consts`` (both splits and escapes resolve
    there), so a prose occurrence of a guarded name — a comment, or a
    longer docstring the name is merely a substring of — is skipped:
    every flagged shape is code, and its vocabulary is visible where
    the gate looks. A marker comment carries ``sv-test-data`` verbatim
    in the text, so it admits on the text alone. A version name is an
    attribute id, or a setenv/setattr argument folded into consts; a
    rate-call name is an identifier and the ARGUMENT is a wanted row
    folded into consts — the clause demands that const evidence beside
    the call's name, because ``resolve`` is spelled by every
    ``Path(...).resolve()`` caller while a flaggable site always pairs
    the name with a wanted-literal argument (issue #715). A source
    that does not compile raises out of the gate: loud by
    construction, and collection fails on the same file anyway.

    Not covered: vocabulary spelled only inside a dead branch
    (``if 0:``). A split or escaped spelling there is unreachable
    short of steganography, but since #715 a PLAIN literal there is
    skipped too: dead-code elimination drops the constant while the
    call's name survives in co_names, so the new const-evidence
    clause never fires. The shape stays loud — the AST sees the
    constant and consts does not, which is exactly the drift
    test_scan_gate's tree property trips on if such a literal ever
    lands under tests/. test_scan_gate's property fails loudly if the
    interpreter's folding changes.
    """
    if "sv-test-data" in source:
        return True
    consts, names = scan_gate.module_vocab(source)
    # Each clause matches the way its shape matches: the version names
    # by membership (setattr/setenv compare the whole argument), the
    # rate-call names by membership in names WITH a wanted row as a
    # string constant — the call's function is an identifier, and the
    # argument a detector would flag is exactly such a const, so the
    # pair is the admission's evidence (issue #715), the path bind by
    # substring (detect folds a substring test over the value), the
    # module reference by attribute name. Empty wanted sets can hold
    # no live-call site, so with the defaults the rate clause admits
    # nothing — default-set behavior this clause introduced; the old
    # clause admitted on the call's name alone.
    return (any(v in consts or v in names for v in VERSION_NAMES)
            or (any(r in names for r in RATE_CALL_NAMES)
                and any(_names_wanted_row(c, wanted_models, wanted_hosts)
                        for c in consts))
            or any("pricing.json" in c for c in consts)
            or "PRICING_JSON" in names)


def check(source: str, *, wanted_models: frozenset[str] = frozenset(),
          wanted_hosts: frozenset[str] = frozenset()) -> list[str]:
    """Every problem in one module's source, as messages naming the line.

    A flagged site of either family without an allow marker on its own
    line, and a marker on a line that carries no flagged site of ANY
    family (marker rot), are both problems. The gate (``_scan_admits``)
    first asks whether the source can hold either at all and skips the
    walk and tokenizer when it cannot — most tree modules cannot, which
    is what keeps this pinned scan from re-pricing with tree size.
    """
    if not _scan_admits(source, wanted_models=wanted_models,
                        wanted_hosts=wanted_hosts):
        return []
    sites = detect(source, wanted_models=wanted_models,
                   wanted_hosts=wanted_hosts)
    site_lines = {site.line for site in sites}
    # A marker comment spells "sv-test-data" verbatim (the MARKER regex
    # requires it), so a source carrying no site and no marker substring
    # has nothing to excuse and nothing to rot, and reading its markers
    # back would tokenize the whole module for nothing (issue #675):
    # every flagged shape is caught exactly as before, the tokenizer
    # just does not run where its verdict is already decided.
    marker_lines = (set()
                    if not sites and "sv-test-data" not in source
                    else _marker_lines(source))
    problems = [_problem(site) for site in sites
                if site.line not in marker_lines]
    problems += [f"line {line}: sv-test-data marker on a line with no "
                 "flagged site (marker rot)"
                 for line in sorted(marker_lines - site_lines)]
    return problems


def _problem(site: Site) -> str:
    """One site's problem message, naming the line and the shape."""
    if site.shape in _LIVE_ROW_SHAPES:
        return f"line {site.line}: {_LIVE_ROW_TEXT[site.shape]}"
    return (f"line {site.line}: assigns a literal to {site.name} "
            f"({site.shape}); add '# sv-test-data: allow' with a reason, "
            "or derive the value from the current constant")


def test_setattr_positional_literal_is_flagged():
    result = detect("monkeypatch.setattr(constants, 'PARSER_VERSION', '2')\n")
    assert [site.name for site in result] == ["PARSER_VERSION"]
    assert result[0].line == 1
    assert result[0].value == "2"
    assert result[0].shape == "setattr"


def test_setattr_keyword_value_literal_is_flagged():
    result = detect(
        "monkeypatch.setattr(constants, 'PRICING_VERSION', value='9')\n")
    assert [(site.name, site.value) for site in result] == [
        ("PRICING_VERSION", "9")]


def test_bare_setattr_literal_is_flagged():
    result = detect("setattr(constants, 'MARKER_READER_VERSION', '2')\n")
    assert [(site.name, site.value) for site in result] == [
        ("MARKER_READER_VERSION", "2")]


def test_direct_constant_assignment_is_flagged():
    result = detect("constants.PARSER_VERSION = '82'\n")
    assert [(site.name, site.shape) for site in result] == [
        ("PARSER_VERSION", "assign")]


def test_setenv_with_a_version_name_is_flagged():
    result = detect("monkeypatch.setenv('PARSER_VERSION', '999')\n")
    assert [(site.name, site.shape, site.value) for site in result] == [
        ("PARSER_VERSION", "setenv", "999")]


def test_setenv_with_an_unrelated_name_is_not_flagged():
    assert detect("monkeypatch.setenv('R2_BUCKET', 'claude')\n") == []


def test_setenv_with_a_derived_value_is_not_flagged():
    assert detect(
        "monkeypatch.setenv('PARSER_VERSION', str(int(n) + 1))\n") == []


def test_derived_value_is_never_flagged():
    source = ("monkeypatch.setattr(constants, 'PRICING_VERSION',\n"
              "    str(int(constants.PRICING_VERSION) + 1))\n")
    assert detect(source) == []


def test_non_version_constant_names_are_not_flagged():
    assert detect("monkeypatch.setattr(constants, '__file__', 'x')\n") == []
    assert detect("constants.OTHER_VERSION = '1'\n") == []
    assert detect(
        "monkeypatch.setattr(constants, 'VERSION', 'x')\n") == []


def test_non_literal_values_are_not_flagged():
    assert detect(
        "monkeypatch.setattr(constants, 'PARSER_VERSION', 81)\n") == []
    assert detect(
        "monkeypatch.setattr(constants, 'PARSER_VERSION', name)\n") == []


def test_stored_version_row_values_are_not_flagged():
    """A stored-version row's value is data, not a constant assignment."""
    assert detect(
        "_seed(c, key, 1, pricing_version='abc')\n") == []


def test_marker_on_the_site_line_excuses_it():
    source = ("monkeypatch.setattr(constants, 'PARSER_VERSION', '2')  "
              "# sv-test-data: allow (synthetic)\n")
    assert check(source) == []
    assert [site.name for site in detect(source)] == ["PARSER_VERSION"]


def test_site_without_a_marker_fails_check():
    problems = check("monkeypatch.setenv('PARSER_VERSION', '999')\n")
    assert len(problems) == 1
    assert "line 1" in problems[0]
    assert "PARSER_VERSION" in problems[0]


def test_marker_on_a_line_without_a_site_is_rot():
    source = ("x = 1  # sv-test-data: allow (gone)\n"
              "monkeypatch.setenv('PARSER_VERSION', '999')\n")
    problems = check(source)
    assert len(problems) == 2
    assert "line 2" in problems[0] and "PARSER_VERSION" in problems[0]
    assert "line 1" in problems[1] and "rot" in problems[1]


def test_marker_text_inside_a_string_is_not_a_marker():
    source = ("CODE = \"monkeypatch.setenv('PARSER_VERSION', '9')  "
              "# sv-test-data: allow (sample)\"\n"
              "monkeypatch.setenv('PARSER_VERSION', '9')\n")
    problems = check(source)
    assert len(problems) == 1
    assert "line 2" in problems[0]


def test_marker_text_inside_the_value_cannot_self_excuse():
    """Marker text inside a value literal is not a comment on its line."""
    source = ("monkeypatch.setattr(constants, 'PARSER_VERSION',\n"
              "    'x  # sv-test-data: allow (self)')\n")
    problems = check(source)
    assert len(problems) == 1
    assert "line 2" in problems[0]


def test_multiline_statement_marks_on_the_literal_line():
    source = ("monkeypatch.setattr(\n"
              "    constants, 'PARSER_VERSION',\n"
              "    '2')  # sv-test-data: allow (synthetic)\n")
    assert check(source) == []


def test_multiline_marker_on_an_inner_line_is_rot():
    source = ("monkeypatch.setattr(\n"
              "    constants, 'PARSER_VERSION', '2')\n"
              "unused = 1  # sv-test-data: allow (misplaced)\n")
    problems = check(source)
    assert len(problems) == 2
    assert "line 2" in problems[0]
    assert "line 3" in problems[1] and "rot" in problems[1]


# --- the live-row family: the committed-document and rate-row sites ---

WANTED_MODELS = frozenset({"acme/acme-9", "claude-sonnet-5"})
WANTED_HOSTS = frozenset({"HostCo"})


def test_rate_call_with_a_wanted_model_literal_is_flagged():
    result = detect("rate_for('acme/acme-9')\n",
                    wanted_models=WANTED_MODELS, wanted_hosts=WANTED_HOSTS)
    assert [(site.name, site.shape, site.value) for site in result] == [
        ("rate_for", "live-call", "acme/acme-9")]


def test_rate_call_normalises_the_model_literal():
    result = detect("rate_for('acme/acme.9')\n",
                    wanted_models=WANTED_MODELS, wanted_hosts=WANTED_HOSTS)
    assert [(site.name, site.value) for site in result] == [
        ("rate_for", "acme/acme.9")]


def test_rate_call_folds_suffixed_model_spellings():
    result = detect("rate_for('claude-sonnet-5[1m]')\n",
                    wanted_models=WANTED_MODELS, wanted_hosts=WANTED_HOSTS)
    assert [(site.name, site.value) for site in result] == [
        ("rate_for", "claude-sonnet-5[1m]")]


def test_resolve_call_with_a_wanted_model_is_flagged():
    result = detect("pricing.resolve('acme/acme-9', ts)\n",
                    wanted_models=WANTED_MODELS, wanted_hosts=WANTED_HOSTS)
    assert [(site.name, site.value) for site in result] == [
        ("resolve", "acme/acme-9")]


def test_rate_call_with_a_wanted_host_keyword_is_flagged():
    result = detect("pricing.resolve(model, provider='HostCo')\n",
                    wanted_models=WANTED_MODELS, wanted_hosts=WANTED_HOSTS)
    assert [(site.name, site.value) for site in result] == [
        ("resolve", "HostCo")]


def test_host_literal_in_another_rate_call_is_flagged():
    result = detect("compute_cost('HostCo', tokens)\n",
                    wanted_models=WANTED_MODELS, wanted_hosts=WANTED_HOSTS)
    assert [(site.name, site.value) for site in result] == [
        ("compute_cost", "HostCo")]


def test_rate_call_with_a_variable_argument_is_not_flagged():
    assert detect("rate_for(model)\n",
                  wanted_models=WANTED_MODELS,
                  wanted_hosts=WANTED_HOSTS) == []


def test_rate_call_with_an_unknown_model_literal_is_not_flagged():
    assert detect("rate_for('acme-ghost-9')\n",
                  wanted_models=WANTED_MODELS,
                  wanted_hosts=WANTED_HOSTS) == []


def test_rate_call_of_a_differently_named_function_is_not_flagged():
    assert detect("price_for('acme/acme-9')\n",
                  wanted_models=WANTED_MODELS,
                  wanted_hosts=WANTED_HOSTS) == []


def test_pricing_json_reference_is_flagged():
    result = detect("pricing.PRICING_JSON.read_text()\n")
    assert [(site.name, site.shape, site.value, site.line)
            for site in result] == [
        ("PRICING_JSON", "pricing-json", "pricing.PRICING_JSON", 1)]


def test_pricing_json_off_the_pricing_module_is_not_flagged():
    assert detect("PRICING_JSON.read_text()\n") == []


def test_module_level_doc_path_bind_is_flagged():
    source = "PRICING_JSON = ROOT / 'src' / 'pricing.json'\n"
    result = detect(source)
    assert [(site.name, site.shape, site.value) for site in result] == [
        ("PRICING_JSON", "path-bind", "pricing.json")]


def test_module_level_ann_assign_path_bind_is_flagged():
    source = "P: Path = ROOT / 'src' / 'pricing.json'\n"
    result = detect(source)
    assert [(site.name, site.shape, site.value) for site in result] == [
        ("P", "path-bind", "pricing.json")]


def test_function_local_doc_path_bind_is_not_flagged():
    source = ("def make(tmp):\n"
              "    p = tmp / 'src' / 'pricing.json'\n"
              "    return p\n")
    assert detect(source) == []


def test_path_bind_problem_names_its_fragment():
    problems = check("PRICING_JSON = ROOT / 'src' / 'pricing.json'\n")
    assert len(problems) == 1
    assert "pricing-json path bind" in problems[0]


def test_live_call_problem_names_its_fragment():
    problems = check("rate_for('acme/acme-9')\n",
                     wanted_models=WANTED_MODELS, wanted_hosts=WANTED_HOSTS)
    assert len(problems) == 1
    assert "live model or host in a pricing call" in problems[0]


def test_marker_excuses_a_live_row_site():
    source = ("doc = pricing.PRICING_JSON.read_text()  "
              "# sv-test-data: allow (synthetic doc)\n")
    assert check(source) == []


def test_rot_fails_when_a_live_row_site_is_removed():
    source = ("x = 1  # sv-test-data: allow (gone)\n"
              "doc = pricing.PRICING_JSON.read_text()\n")
    problems = check(source)
    assert len(problems) == 2
    assert "line 2" in problems[0] and "pricing document" in problems[0]
    assert "line 1" in problems[1] and "rot" in problems[1]


_DEPLOYED_WANTED: tuple[frozenset[str], frozenset[str]] | None = None


def _deployed_wanted() -> tuple[frozenset[str], frozenset[str]]:
    """The live sets the DEPLOYED document defines, read in a subprocess.

    Two reasons not to read the process's own loaded tables: the
    suite-cost bench rebinds them to a bounded document before its
    counted windows open (SV-CI-RATCHETS), so a wanted set read off the
    loaded tables would shrink under the bench and rot every marker
    pinned to a real row; and parsing the real document in-process
    would put its parse into this suite's counted run phase. The
    subprocess pays the parse outside every counted window.
    """
    global _DEPLOYED_WANTED
    if _DEPLOYED_WANTED is None:
        code = (
            "import json, sys; sys.path.insert(0, %r); "
            "from backend import pricing_load as pl; "
            "print(json.dumps([sorted({*pl.MODEL_RATES, *pl.VENDOR_BARE}), "
            "sorted({h for _, h in pl.PROVIDER_RATES})]))" % str(REPO_ROOT))
        result = subprocess.run(  # pylint: disable=subprocess-run-check
            [sys.executable, "-c", code], capture_output=True, text=True,
            check=True, timeout=120)
        models, hosts = json.loads(result.stdout)
        _DEPLOYED_WANTED = (frozenset(models), frozenset(hosts))
    return _DEPLOYED_WANTED


def _tree_problems(tests_dir: Path) -> list[str]:
    """check() over a whole tree, as ``path:message`` strings. The wanted
    models are the merged view's: models-table keys and tracked vendor
    bare keys alike (a live rate call names either)."""
    wanted_models, wanted_hosts = _deployed_wanted()
    problems = []
    for path in sorted(tests_dir.rglob("*.py")):
        for message in check(path.read_text(encoding="utf-8"),
                             wanted_models=wanted_models,
                             wanted_hosts=wanted_hosts):
            problems.append(f"{path.name}:{message}")
    return problems


def test_the_tests_tree_holds_no_unmarked_site_and_no_rotted_marker():
    assert not _tree_problems(REPO_ROOT / "tests"), (
        "tests/ holds a site the guard flags, or a rotted marker — "
        + "\n".join(_tree_problems(REPO_ROOT / "tests")))


def test_a_seeded_version_literal_is_flagged_in_a_fresh_file(tmp_path):
    (tmp_path / "test_planted.py").write_text(
        "def test_uses_a_version_constant(monkeypatch):\n"
        "    monkeypatch.setenv('PARSER_VERSION', '999')\n", encoding="utf-8")
    problems = _tree_problems(tmp_path)
    assert len(problems) == 1 and "line 2" in problems[0]


def test_a_seeded_non_plain_spelled_literal_is_flagged(tmp_path):
    # Split-adjacent and escape-spelled literals: the text never spells
    # the name, the compiler's consts do (splits and escapes both
    # resolve there).
    header = "def test_uses_a_version_constant(monkeypatch):\n    "
    plants = (
        header + "monkeypatch.setenv('PARSER' '_VERSION', '999')\n",
        header + "monkeypatch.setenv('PARSER\\x5fVERSION', '999')\n")
    for number, plant in enumerate(plants):
        (tmp_path / f"test_planted_{number}.py").write_text(
            plant, encoding="utf-8")
    problems = _tree_problems(tmp_path)
    assert len(problems) == 2 and all("line 2" in p for p in problems)


def test_a_seeded_marker_without_a_site_is_rot(tmp_path):
    (tmp_path / "test_planted.py").write_text(
        "X = 1  # sv-test-data: allow (rot)\n", encoding="utf-8")
    problems = _tree_problems(tmp_path)
    assert len(problems) == 1 and "rot" in problems[0]


def test_a_seeded_live_rate_call_is_flagged_in_a_fresh_file(tmp_path):
    # The dotted spelling: the text clause admits on the rate call's
    # name alone, and detect() normalises the literal before matching,
    # so the plant is flagged whatever the literal's spelling. The
    # plant names a DEPLOYED row, the same set the scanner matches.
    model = sorted(_deployed_wanted()[0])[0]
    dotted = model.replace("-", ".")
    (tmp_path / "test_planted.py").write_text(
        "def test_prices_a_live_row():\n"
        f"    return rate_for('{dotted}')\n", encoding="utf-8")
    problems = _tree_problems(tmp_path)
    assert len(problems) == 1 and "line 2" in problems[0]


def test_a_seeded_host_literal_is_flagged_case_exactly(tmp_path):
    host = sorted(_deployed_wanted()[1])[0]
    (tmp_path / "test_planted.py").write_text(
        "def test_prices_a_host():\n"
        f"    return resolve(model, '{host}')\n", encoding="utf-8")
    problems = _tree_problems(tmp_path)
    assert len(problems) == 1 and "line 2" in problems[0]


def test_a_seeded_split_path_bind_in_a_longer_literal_is_flagged(tmp_path):
    # The substring clause's plant: a split spelling folded into a
    # LONGER constant, which neither the text nor an equality test over
    # consts carries — detect matches it as a substring, so the gate
    # must too.
    (tmp_path / "test_planted.py").write_text(
        "DOC = ROOT / 'src/pricing' '.json'\n", encoding="utf-8")
    problems = _tree_problems(tmp_path)
    assert len(problems) == 1 and "path bind" in problems[0]


def test_a_module_without_the_scan_vocabulary_is_skipped():
    # The structural cost pin (issue #510): a source that names
    # nothing the scanner can flag is not walked at all.
    source = "import os\n\n\ndef test_ok():\n    return os.sep\n"
    assert check(source) == []
    assert not _scan_admits(source)


def test_a_site_free_marker_free_module_is_not_tokenized(monkeypatch):
    # The second structural cost pin (issue #675): an ADMITTED source
    # that carries no flagged site and no marker text pays no tokenizer
    # pass. A marker comment spells ``sv-test-data`` verbatim (the
    # MARKER regex requires it), so a source without that substring has
    # nothing to excuse and nothing to rot, and reading its markers
    # back would tokenize the whole module for nothing — the pass that
    # made check() cost per line. The walk detect() needs stays; the
    # monkeypatch only proves the tokenizer does not.
    source = ("M = 'acme/acme-9'\n\n\n"
              "def test_prices(model):\n    return resolve(model)\n")
    assert _scan_admits(source, wanted_models=WANTED_MODELS,
                        wanted_hosts=WANTED_HOSTS)

    calls = []
    with monkeypatch.context() as m:
        m.setattr(sys.modules[__name__], "_marker_lines",
                  lambda src: calls.append(src) or set())
        assert check(source, wanted_models=WANTED_MODELS,
                     wanted_hosts=WANTED_HOSTS) == []
        assert not calls
    # And a source WITH marker text still pays for it: the short-
    # circuit is on the marker's own substring, never a silent drop —
    # the rot verdict is the tokenizer's own, unpatched.
    marked = source + "x = 1  # sv-test-data: allow (rot)\n"
    assert check(marked, wanted_models=WANTED_MODELS,
                 wanted_hosts=WANTED_HOSTS) == [
        "line 6: sv-test-data marker on a line with no flagged site "
        "(marker rot)"]


def test_a_prose_rate_or_version_name_is_not_admitted():
    # Issue #679: admission is compile-level — an identifier lands in
    # co_names and a string constant in co_consts, so a comment, or a
    # docstring the name is merely a substring of, can hold nothing
    # detect() flags (the scan is AST-based) and admits no longer:
    # admitting it on raw text was pure parse-and-walk cost. The
    # code-shape admission is pinned by the seeded-literal tests:
    # test_a_seeded_version_literal_is_flagged_in_a_fresh_file and
    # test_a_seeded_live_rate_call_is_flagged_in_a_fresh_file. A
    # source that does not compile raises out of the gate now: the old
    # text clauses skipped broken files silently, module_vocab's
    # compile does not — loud, and collection fails on the same file
    # anyway.
    assert not _scan_admits("x = 1  # then resolve which model answers\n")
    assert not _scan_admits(
        '"""Resolve usage; mentions PARSER_VERSION in prose."""\n'
        "x = 1\n")


def test_a_rate_call_without_a_wanted_const_is_not_admitted():
    # Issue #715: the rate-call clause admitted on the call's name
    # alone, so any module whose code merely spells ``resolve`` — every
    # ``Path(...).resolve()`` caller — paid the full AST walk while
    # nothing flaggable existed. A flaggable live-call site needs a
    # string constant naming a wanted row (the detector's own
    # membership test), so admission now demands that const evidence:
    # the clause stays a strict superset of what detect() can flag.
    source = ("from pathlib import Path\n\n\n"
              "def test_ok():\n    return Path('.').resolve()\n")
    assert not _scan_admits(source, wanted_models=WANTED_MODELS,
                            wanted_hosts=WANTED_HOSTS)
    assert check(source, wanted_models=WANTED_MODELS,
                 wanted_hosts=WANTED_HOSTS) == []


def test_a_rate_call_admits_only_with_a_wanted_const():
    # The complement: the same call shape carrying a wanted literal is
    # still admitted and still flagged — the const evidence is what
    # both the gate and the detector read.
    call = "def test_prices():\n    return resolve(model='acme/acme-9')\n"
    assert _scan_admits(call, wanted_models=WANTED_MODELS,
                        wanted_hosts=WANTED_HOSTS)
    assert len(check(call, wanted_models=WANTED_MODELS,
                     wanted_hosts=WANTED_HOSTS)) == 1


# --- issue #829: the wanted-row fold is rows-independent ---

def _synthetic_wanted(count: int) -> frozenset[str]:
    """A synthetic wanted set exercising every fold shape (SV-TEST-DATA:
    no live row is named). ``acme-model`` prefixes the whole family, so
    the longest-key fold has real work."""
    return frozenset(
        ["acme-model"] + [f"acme-model-{n}" for n in range(count)])


_SYNTHETIC_QUERIES = (
    "acme-model", "acme-model-7", "acme-model-7[1m]", "acme-model-7@x",
    "acme-model-7-20250514", "acme-model-7-202505", "acme-model-77",
    "acme-x", "acme-model-7-20250514[1m]", "acme-model-7.2025",
    "ACME-MODEL-7.X", "x-acme-model-7", "")


def test_match_wanted_agrees_with_the_literal_scan():
    """The fold over a parameter set answers exactly what the literal
    O(|wanted|) scan answers, whatever the query."""
    wanted = _synthetic_wanted(40)
    for query in _SYNTHETIC_QUERIES:
        norm = pricing._normalise(query)  # pylint: disable=protected-access
        assert _match_wanted_scan(norm, wanted) == _match_wanted(
            norm, wanted), query


def test_the_fold_matches_pricings_own_key_fold():
    # The rest-validity predicate the fold mirrors lives in production
    # too (pricing._match_key over MODEL_RATES); nothing else pins this
    # module's copy against it, so a new accepted tail shape there
    # would drift the guard silently.
    key = sorted(pricing.MODEL_RATES)[0]
    for query in ("", "x", key, key + "[1m]", key + "-20250514",
                  key + "@x"):
        assert (_match_wanted(query, frozenset(pricing.MODEL_RATES))
                == pricing._match_key(query)), query  # pylint: disable=protected-access


def test_names_wanted_row_never_pays_the_scan(monkeypatch):
    """The hot path is candidate lookup, not the linear scan: the tree
    scan pays ``_names_wanted_row`` once per string constant under
    tests/, so a per-call scan would price the guard by the committed
    document's row count and the suite-cost run phase would move on
    every pricing refresh (issue #829)."""
    def _boom(*_args, **_kwargs):
        raise AssertionError("the linear wanted-row scan ran")

    monkeypatch.setattr(version_literal_guard, "_match_wanted_scan", _boom)
    wanted = _synthetic_wanted(4)
    hosts = frozenset({"HostCo"})
    assert _names_wanted_row("acme-model-2", wanted, hosts)
    assert _names_wanted_row("acme-model-2[1m]", wanted, hosts)
    assert _names_wanted_row("acme-model-2@x", wanted, hosts)
    assert _names_wanted_row("acme-model-2-20250514", wanted, hosts)
    assert not _names_wanted_row("acme-ghost", wanted, hosts)
    assert _names_wanted_row("HostCo", frozenset(), hosts)
