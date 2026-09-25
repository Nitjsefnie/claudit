"""The patch-coverage readout module: scripts/ci/diff_coverage.py.

The readout answers one question for a pull request: of the lines the
change ADDED, how many did the test suite execute. It is INFORMATIONAL —
the module exits 0 for any result it could compute, even 0%, and nonzero
only for a hard setup error (a missing file, unparseable input). The
workflow shape that wires it is pinned in
tests/test_workflow_patch_coverage.py.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

MODULE_PATH = ROOT / "scripts" / "ci" / "diff_coverage.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("diff_coverage", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["diff_coverage"] = module
    spec.loader.exec_module(module)
    return module


dc = _load_module()


def _write_coverage(tmp_path: Path, lines: str) -> Path:
    xml = f"""<?xml version="1.0" ?>
<coverage line-rate="0.500" branch-rate="0.000" version="7.6.1">
  <sources>
    <source>/repo</source>
  </sources>
  <packages>
    <package name="." line-rate="0.500" branch-rate="0.000" complexity="0">
      <classes>
        <class name="mod.py" filename="backend/mod.py" complexity="0">
          <methods/>
          <lines>
{lines}
          </lines>
        </class>
      </classes>
    </package>
  </packages>
</coverage>
"""
    path = tmp_path / "coverage.xml"
    path.write_text(xml, encoding="utf-8")
    return path


def _write_diff(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "pr.diff"
    path.write_text(body, encoding="utf-8")
    return path


FULL_LINE_DIFF = """\
diff --git a/backend/mod.py b/backend/mod.py
index 1111111..2222222 100644
--- a/backend/mod.py
+++ b/backend/mod.py
@@ -1,3 +1,5 @@
 keep
+added_one
+added_two
 keep
"""


class TestCoverageXmlParsing:
    def test_full_line_covered(self, tmp_path):
        """An added line with hits executes: it is covered, not partial."""
        xml = _write_coverage(tmp_path, '            <line number="2" hits="1"/>')
        measured = dc.executable_lines(xml)
        assert measured == {"backend/mod.py": {2: dc.Line(hits=1, partial=False)}}

    def test_partial_line_via_partial_attribute(self, tmp_path):
        """partial="true" marks a line executed on some branches only."""
        xml = _write_coverage(
            tmp_path,
            '            <line number="2" hits="1" partial="true"/>')
        measured = dc.executable_lines(xml)
        assert measured["backend/mod.py"][2] == dc.Line(hits=1, partial=True)

    def test_partial_line_via_condition_coverage(self, tmp_path):
        """Real `coverage xml` spells partial as a sub-100% condition-coverage."""
        xml = _write_coverage(
            tmp_path,
            '            <line number="2" hits="1" branch="true"'
            ' condition-coverage="50% (1/2)"/>')
        measured = dc.executable_lines(xml)
        assert measured["backend/mod.py"][2] == dc.Line(hits=1, partial=True)

    def test_full_condition_coverage_is_not_partial(self, tmp_path):
        """A branch line whose conditions all ran is plain covered."""
        xml = _write_coverage(
            tmp_path,
            '            <line number="2" hits="3" branch="true"'
            ' condition-coverage="100% (2/2)"/>')
        measured = dc.executable_lines(xml)
        assert measured["backend/mod.py"][2] == dc.Line(hits=3, partial=False)

    def test_repeated_class_entries_take_the_best_hits(self, tmp_path):
        """A file may appear as more than one <class>; a line reached by
        any of them is covered."""
        xml_path = tmp_path / "coverage.xml"
        xml_path.write_text(
            """<?xml version="1.0" ?>
<coverage line-rate="1.000" version="7.6.1">
  <sources><source>/repo</source></sources>
  <packages><package name="." line-rate="1.000" complexity="0">
    <classes>
      <class name="mod.py" filename="backend/mod.py" complexity="0">
        <methods/>
        <lines>
          <line number="2" hits="0"/>
        </lines>
      </class>
      <class name="mod.py" filename="backend/mod.py" complexity="0">
        <methods/>
        <lines>
          <line number="2" hits="2"/>
        </lines>
      </class>
    </classes>
  </package></packages>
</coverage>
""",
            encoding="utf-8")
        measured = dc.executable_lines(xml_path)
        assert measured["backend/mod.py"][2] == dc.Line(hits=2, partial=False)

    def test_unparseable_xml_raises(self, tmp_path):
        xml = tmp_path / "coverage.xml"
        xml.write_text("<coverage><unclosed>", encoding="utf-8")
        with pytest.raises(dc.InputError):
            dc.executable_lines(xml)

    def test_non_coverage_root_raises(self, tmp_path):
        xml = tmp_path / "coverage.xml"
        xml.write_text("<somethingelse/>", encoding="utf-8")
        with pytest.raises(dc.InputError):
            dc.executable_lines(xml)

    def test_missing_line_number_raises(self, tmp_path):
        xml = _write_coverage(tmp_path, '            <line hits="1"/>')
        with pytest.raises(dc.InputError):
            dc.executable_lines(xml)

    def test_zero_line_number_raises(self, tmp_path):
        xml = _write_coverage(tmp_path, '            <line number="0" hits="1"/>')
        with pytest.raises(dc.InputError):
            dc.executable_lines(xml)

    def test_no_usable_line_entries_raises(self, tmp_path):
        xml = _write_coverage(tmp_path, "")
        with pytest.raises(dc.InputError):
            dc.executable_lines(xml)


class TestDiffParsing:
    def test_added_lines_are_numbered_within_the_hunk(self, tmp_path):
        diff = _write_diff(tmp_path, FULL_LINE_DIFF)
        added = dc.added_lines(diff.read_text(encoding="utf-8"))
        assert added == {"backend/mod.py": {2, 3}}

    def test_removed_lines_move_nothing(self, tmp_path):
        diff = _write_diff(
            tmp_path,
            """\
diff --git a/backend/mod.py b/backend/mod.py
--- a/backend/mod.py
+++ b/backend/mod.py
@@ -1,3 +1,3 @@
 keep
-gone
+back
 keep
""")
        added = dc.added_lines(diff.read_text(encoding="utf-8"))
        assert added == {"backend/mod.py": {2}}

    def test_file_deletion_names_no_path(self, tmp_path):
        """A deleted file's diff target is /dev/null; it adds no lines."""
        diff = _write_diff(
            tmp_path,
            """\
diff --git a/backend/gone.py b/backend/gone.py
deleted file mode 100644
--- a/backend/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-gone_one
-gone_two
""")
        added = dc.added_lines(diff.read_text(encoding="utf-8"))
        assert added == {}

    def test_non_python_paths_are_ignored(self, tmp_path):
        """The readout measures Python only; other paths never reach it."""
        diff = _write_diff(
            tmp_path,
            """\
diff --git a/src/app.jsx b/src/app.jsx
--- a/src/app.jsx
+++ b/src/app.jsx
@@ -1,2 +1,3 @@
 keep
+jsx_line
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,2 +1,3 @@
 keep
+md_line
""")
        added = dc.added_lines(diff.read_text(encoding="utf-8"))
        assert added == {"src/app.jsx": {2}, "README.md": {2}}
        rows, covered, partial, total = dc.measure(
            dc.executable_lines(_write_coverage(
                tmp_path, '            <line number="2" hits="1"/>')),
            added)
        assert (rows, covered, partial, total) == ([], 0, 0, 0)

    def test_binary_diff_record_is_rejected(self, tmp_path):
        diff = _write_diff(
            tmp_path,
            """\
diff --git a/backend/logo.png b/backend/logo.png
Binary files a/backend/logo.png and b/backend/logo.png differ
""")
        with pytest.raises(dc.InputError):
            dc.added_lines(diff.read_text(encoding="utf-8"))

    def test_removed_line_starting_with_dashes_is_not_a_header(self, tmp_path):
        """Git renders a REMOVED line whose content begins `-- ` as
        `--- ...`; that is hunk body, not a file header."""
        diff = _write_diff(
            tmp_path,
            """\
diff --git a/backend/mod.py b/backend/mod.py
--- a/backend/mod.py
+++ b/backend/mod.py
@@ -1,3 +1,3 @@
 keep
--- a/backend/mod.py
+back
 keep
""")
        added = dc.added_lines(diff.read_text(encoding="utf-8"))
        assert added == {"backend/mod.py": {2}}

    def test_git_quoted_path_is_decoded(self, tmp_path):
        """An exotic filename arrives quoted with octal escapes; `b/` is
        the diff-side prefix, not part of the name."""
        diff = _write_diff(
            tmp_path,
            'diff --git "a/backend/m\\303\\266d.py" "b/backend/m\\303\\266d.py"\n'
            "--- \"a/backend/m\\303\\266d.py\"\n"
            "+++ \"b/backend/m\\303\\266d.py\"\n"
            "@@ -1,2 +1,3 @@\n"
            " keep\n"
            "+neu\n")
        added = dc.added_lines(diff.read_text(encoding="utf-8"))
        assert added == {"backend/m\xc3\xb6d.py": {2}}


class TestMeasure:
    def _measured(self, tmp_path):
        xml = _write_coverage(
            tmp_path,
            '            <line number="2" hits="1"/>\n'
            '            <line number="3" hits="1" partial="true"/>\n'
            '            <line number="4" hits="0"/>\n'
            '            <line number="5" hits="0"/>')
        return dc.executable_lines(xml)

    def test_buckets(self, tmp_path):
        added = {"backend/mod.py": {2, 3, 4}}
        rows, covered, partial, total = dc.measure(self._measured(tmp_path), added)
        assert len(rows) == 1
        path, c, p, m, missed = rows[0]
        assert path == "backend/mod.py"
        assert (c, p, m) == (1, 1, 1)
        assert missed == [4]
        assert (covered, partial, total) == (1, 1, 3)

    def test_added_non_executable_line_is_excluded(self, tmp_path):
        """A line the XML does not list in a measured file is not an
        executable statement (blank, comment, `else:`); absence from the
        denominator keeps the number independent of formatting."""
        added = {"backend/mod.py": {2, 99}}
        rows, covered, partial, total = dc.measure(self._measured(tmp_path), added)
        assert (covered, partial, total) == (1, 1, 2)
        assert rows[0].missed == [4]

    def test_file_with_no_coverage_data_is_excluded(self, tmp_path):
        """A backend file the XML does not name is out of the denominator —
        absence is not a miss — and is named by unmeasured_scope."""
        added = {"backend/newfile.py": {2}, "backend/mod.py": {2}}
        rows, covered, partial, total = dc.measure(self._measured(tmp_path), added)
        assert total == 1
        assert rows[0].path == "backend/mod.py"
        assert dc.unmeasured_scope(self._measured(tmp_path), added) == [
            "backend/newfile.py"]

    def test_no_rows_when_nothing_added_is_measured(self, tmp_path):
        added = {"backend/mod.py": {99}}
        rows, covered, partial, total = dc.measure(self._measured(tmp_path), added)
        assert (rows, covered, partial, total) == ([], 0, 0, 0)


class TestRender:
    def test_table_and_percentage(self, tmp_path):
        rows = [dc.FileRow("backend/mod.py", 1, 1, 1, [4])]
        body = dc.render(rows, 1, 1, 3, unmeasured=[])
        assert "33.3%" in body
        assert "| `backend/mod.py` | 1 | 1 | 1 | 4 |" in body
        assert "partially" in body  # the partial bucket is named

    def test_missed_ranges_collapse(self):
        rows = [dc.FileRow("backend/mod.py", 0, 0, 4, [3, 4, 5, 9])]
        body = dc.render(rows, 0, 0, 4, unmeasured=[])
        assert "3-5, 9" in body

    def test_zero_percent_is_still_a_report(self, tmp_path):
        body = dc.render([], 0, 0, 0, unmeasured=[])
        assert "No" in body  # says why nothing was measured, not "0%"

    def test_unmeasured_backend_files_are_named(self, tmp_path):
        body = dc.render([dc.FileRow("backend/mod.py", 1, 0, 0, [])],
                         1, 0, 1, unmeasured=["backend/newfile.py"])
        assert "`backend/newfile.py`" in body

    def test_full_coverage_says_so(self):
        rows = [dc.FileRow("backend/mod.py", 2, 0, 0, [])]
        body = dc.render(rows, 2, 0, 2, unmeasured=[])
        assert "Every" in body

    def test_scope_note_names_the_measured_tree(self):
        body = dc.render([], 0, 0, 0, unmeasured=[])
        assert "backend/" in body  # only backend/ is measured


class TestMain:
    def test_exit_zero_on_a_computed_zero_result(self, tmp_path, capsys):
        xml = _write_coverage(tmp_path, '            <line number="2" hits="0"/>')
        diff = _write_diff(tmp_path, FULL_LINE_DIFF)
        assert dc.main(["--coverage", str(xml), "--diff", str(diff)]) == 0
        assert "0.0%" in capsys.readouterr().out

    def test_exit_zero_when_the_diff_names_no_python(self, tmp_path, capsys):
        xml = _write_coverage(tmp_path, '            <line number="2" hits="1"/>')
        diff = _write_diff(tmp_path, "diff --git a/x.txt b/x.txt\n--- a/x.txt\n"
                                      "+++ b/x.txt\n@@ -1 +1,2 @@\n keep\n+new\n")
        assert dc.main(["--coverage", str(xml), "--diff", str(diff)]) == 0

    def test_summary_file_receives_the_body(self, tmp_path, capsys):
        xml = _write_coverage(tmp_path, '            <line number="2" hits="1"/>')
        diff = _write_diff(tmp_path, FULL_LINE_DIFF)
        summary = tmp_path / "summary.md"
        assert dc.main(["--coverage", str(xml), "--diff", str(diff),
                        "--summary-file", str(summary)]) == 0
        stdout = capsys.readouterr().out
        assert stdout == summary.read_text(encoding="utf-8")

    def test_missing_coverage_file_exits_nonzero(self, tmp_path, capsys):
        diff = _write_diff(tmp_path, FULL_LINE_DIFF)
        code = dc.main(["--coverage", str(tmp_path / "absent.xml"),
                        "--diff", str(diff)])
        assert code != 0
        assert "coverage" in capsys.readouterr().err.lower()

    def test_missing_diff_file_exits_nonzero(self, tmp_path, capsys):
        xml = _write_coverage(tmp_path, '            <line number="2" hits="1"/>')
        code = dc.main(["--coverage", str(xml),
                        "--diff", str(tmp_path / "absent.diff")])
        assert code != 0
        assert "diff" in capsys.readouterr().err.lower()
