from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from backend import api_export

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_plot_db_module(monkeypatch, request):
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


def test_build_export_argv_all_and_no_project():
    argv = api_export.build_export_argv("all", None, "/tmp/out.png")
    assert "--all" in argv
    assert "-p" not in argv
    assert "--period=" not in argv
    assert "--project" not in argv
    assert "--db-url" not in argv


def test_build_export_argv_dash_slug_survives_plot_parser(monkeypatch, request):
    """Dash-leading project slugs stay values through the real plot parser."""
    mod = _load_plot_db_module(monkeypatch, request)
    for slug in ("-root-claudit", "--double-dash-project"):
        argv = api_export.build_export_argv("7d", slug, "/tmp/out.png")
        args = mod._build_parser().parse_args(  # pylint: disable=protected-access
            argv[2:])
        assert args.project == slug
