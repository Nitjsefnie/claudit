"""The export argv builder's tests, split from test_api.py to keep that
module under its size ceiling — relocation only, no assertion changed."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from backend import api_export

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_plot_db_module(monkeypatch, request):
    """Import plot module by path with helper modules isolated per test."""
    path = _REPO_ROOT / "scripts/plots/ccusage_plot_db.py"
    helper_names = ("ccusage_plot_render", "ccusage_plot_timeline")
    present_before = set(sys.modules).intersection(helper_names)
    for name in helper_names:
        if name not in present_before:
            request.addfinalizer(
                lambda name=name: sys.modules.pop(name, None))
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.syspath_prepend(str(path.parent))
    spec = importlib.util.spec_from_file_location("ccusage_plot_db", path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_build_export_argv_period_and_project():
    argv = api_export.build_export_argv("7d", "myproj", "/tmp/out.png")
    assert "--output=/tmp/out.png" in argv
    assert "--period=7d" in argv
    assert "--project=myproj" in argv
    assert "--db-url" not in argv  # DSN comes from inherited env, not argv
    assert "--all" not in argv


def test_build_export_argv_all_and_no_project():
    argv = api_export.build_export_argv("all", None, "/tmp/out.png")
    assert "--all" in argv
    assert "-p" not in argv
    assert "--period=" not in argv
    assert "--project" not in argv
    assert "--db-url" not in argv


def test_build_export_argv_value_options_use_equals_form():
    """Every value-taking option the export passes rides in --opt=value
    form — a single argv element, so a dash-leading value can never be
    re-read as an option (issue #380)."""
    argv = api_export.build_export_argv("7d", "-root-claudit", "/tmp/out.png")
    assert "--output=/tmp/out.png" in argv
    assert "--period=7d" in argv
    assert "--project=-root-claudit" in argv
    assert not {"--project", "-p", "-o", "--period", "--output"} & set(argv)


def test_build_export_argv_dash_slug_survives_plot_parser(monkeypatch, request):
    """GET /api/export answered 500 for every project id starting with
    '-' — i.e. every POSIX Claude project (ids are path slugs such as
    -root-claudit, some start '--'). In the two-element space form the
    plot script's argparse reads the slug as an unknown option and exits
    2; the --opt=value form keeps it a value. Goes through BOTH halves:
    build_export_argv AND the script's own _build_parser (on unfixed code
    the parse raises SystemExit — that is the RED)."""
    mod = _load_plot_db_module(monkeypatch, request)
    for slug in ("-root-claudit", "--double-dash-project"):
        argv = api_export.build_export_argv("7d", slug, "/tmp/out.png")
        args = mod._build_parser().parse_args(  # pylint: disable=protected-access
            argv[2:])
        assert args.project == slug
