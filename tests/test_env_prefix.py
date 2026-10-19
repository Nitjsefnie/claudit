"""Configuration namespace guard and one-release migration contract; it checks
accidental unprefixed reads via `os.environ.get(...)`, `os.environ[...]`, and
`os.getenv(...)`. Environment aliases, destructuring, reflection with
non-literal names, `eval`, and generated code are out of scope.
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import logging
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from backend import api_export, app, branding, db, flags, guest_login, r2
from backend.flag_registry import COLLECTION_SPECS, FEATURE_SPECS
from tests.test_env_prefix_scripts import SCRIPT_DYNAMIC_SITES, is_script_module, scan_script_reads, script_violations

ROOT = Path(__file__).resolve().parents[1]
PLATFORM_PREFIXES = {
    "GITHUB_": "GitHub Actions runner metadata and output files.",
    "PYTHON": "Interpreter-owned runtime controls.",
}
PLATFORM_NAMES = {
    "IFS": "Shell-owned separator modelled lexically in transcript commands.",
    "NEEDS_JSON": "GitHub Actions job-needs payload, not app configuration.",
    "PR_NUMBER": "GitHub pull-request event field forwarded by the workflow.",
    "BEFORE_SHA": "GitHub push event field forwarded by the workflow.",
    "GF_PR": "GitHub pull-request number forwarded to the freshness check.",
    "GF_HEAD_SHA": "GitHub head SHA forwarded to the freshness check.",
    "GF_HEAD_REPO": "GitHub head repository forwarded to the freshness check.",
}
# Dynamic exemptions pin individual file/line sites and reject unknown expressions.
DYNAMIC_SITES = {
    ("backend/bash_reads.py", 115): "Lexical transcript-shell mapping, not process configuration.",
    ("backend/bash_reads.py", 158): "Lexical transcript-shell mapping, not process configuration.",
    ("backend/branding.py", 54): "_branded callers are scanned as config reads below.",
    ("backend/flags.py", 92): "Registry env names are checked for the prefix below.",
    ("backend/flags.py", 94): "Registry env names are checked for the prefix below.",
    ("backend/env_config.py", 44): "Prefixed name validated by the resolver.",
    ("backend/env_config.py", 45): "Prefixed name validated by the resolver.",
    ("backend/env_config.py", 47): "Legacy name from the checked fallback map, only here.",
    ("backend/env_config.py", 49): "Legacy name from the checked fallback map, only here.",
    ("backend/env_config.py", 55): "Required setting delegates to the validated resolver.",
    ("backend/env_config.py", 69): "Startup warning checks only the fallback map, only here.",
    ("backend/env_config.py", 71): "Recorded fallback uses refer only to checked map names. Import snapshots are retained.",
    ("backend/env_config.py", 86): "Validated prefixed name in the dotenv-aware resolver.",
    ("backend/env_config.py", 87): "Validated prefixed name delegated to getenv.",
    ("backend/env_config.py", 96): "Legacy name comes only from the central fallback map.",
    ("backend/env_config.py", 116): "Dotenv loads arbitrary keys; config consumers are scanned.",
}


_ORIGIN_ATTRIBUTES = {
    "os": {"environ": "mapping", "getenv": "getter"},
    "backend": {"env_config": "config"},
    "config": {"getenv": "getter", "require_env": "getter", "getenv_with_dotenv": "getter"},
    "mapping": {"get": "getter", "setdefault": "getter"},
}


def _reflective_os_origin(node: ast.Call, origins: dict[str, set[str]]) -> set[str]:
    if not (isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) >= 2
            and "os" in _value_origins(node.args[0], origins)):
        return set()
    attribute = node.args[1]
    if isinstance(attribute, ast.Constant) and isinstance(attribute.value, str):
        kind = _ORIGIN_ATTRIBUTES["os"].get(attribute.value)
        return {kind} if kind else set()
    return {"unresolved_os_attribute"}


def _value_origins(node: ast.AST, origins: dict[str, set[str]]) -> set[str]:
    result: set[str] = set()
    if isinstance(node, ast.Name):
        result = origins.get(node.id, set()).copy()
    elif isinstance(node, ast.Attribute):
        result = {_ORIGIN_ATTRIBUTES[kind][node.attr] for kind in _value_origins(node.value, origins)
                  if node.attr in _ORIGIN_ATTRIBUTES.get(kind, {})}
    elif isinstance(node, ast.Call):
        result = _reflective_os_origin(node, origins)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "copy":
            result = _value_origins(node.func.value, origins) & {"mapping"}
        elif isinstance(node.func, ast.Name) and node.func.id == "dict" and node.args:
            result = _value_origins(node.args[0], origins) & {"mapping"}
        # Other calls return data, not a proven alias of their arguments.
    elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        result = set().union(*(_value_origins(child, origins) for child in node.elts))
    elif isinstance(node, ast.Dict):
        result = set().union(*(_value_origins(child, origins) for child in node.values))
    elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        result = _value_origins(node.elt, origins)
    elif isinstance(node, ast.DictComp):
        result = _value_origins(node.value, origins)
    elif isinstance(node, ast.IfExp):
        result = _value_origins(node.body, origins) | _value_origins(node.orelse, origins)
    elif isinstance(node, ast.BoolOp):
        result = set().union(*(_value_origins(child, origins) for child in node.values))
    return result


def _import_origins(node: ast.AST, origins: dict[str, set[str]]) -> None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            kind = {"os": "os", "backend": "backend", "backend.env_config": "config"}.get(alias.name)
            if kind:
                name = alias.asname or alias.name.split(".")[0]
                origins.setdefault(name, set()).add(kind if alias.asname else alias.name.split(".")[0])
    elif isinstance(node, ast.ImportFrom):
        for alias in node.names:
            kind = {("os", "environ"): "mapping", ("os", "getenv"): "getter",
                    ("backend", "env_config"): "config"}.get((node.module or "", alias.name))
            if node.module == "backend.env_config" and alias.name in {"getenv", "require_env", "getenv_with_dotenv"}:
                kind = "getter"
            if kind:
                origins.setdefault(alias.asname or alias.name, set()).add(kind)


def _alias_sources(node: ast.AST) -> list[tuple[ast.AST, ast.AST]]:
    if isinstance(node, ast.Assign):
        return [(target, node.value) for target in node.targets]
    if isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)) and node.value is not None:
        return [(node.target, node.value)]
    if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        return [(node.target, node.iter)]
    if isinstance(node, ast.withitem) and node.optional_vars is not None:
        return [(node.optional_vars, node.context_expr)]
    return []


def _alias_pass(nodes: tuple[ast.AST, ...], origins: dict[str, set[str]]) -> bool:
    changed = False
    for node in nodes:
        for target, value in _alias_sources(node):
            kinds = _value_origins(value, origins)
            if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)) and not isinstance(value, (ast.List, ast.Tuple, ast.Set)):
                # Iterating an environment mapping binds string keys, not aliases.
                kinds -= {"mapping"}
            for bound in ast.walk(target):
                if isinstance(bound, ast.Name):
                    previous = origins.setdefault(bound.id, set())
                    if not kinds <= previous:
                        previous.update(kinds)
                        changed = True
    return changed


def _string_bound_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.arg):
        return {node.arg}
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                         ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
        return {node.name} if node.name else set()
    if isinstance(node, ast.MatchMapping):
        return {node.rest} if node.rest else set()
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return set(node.names)
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return {alias.asname or alias.name.split(".")[0] for alias in node.names}
    return set()


def _constant_values(nodes: tuple[ast.AST, ...]) -> dict[str, str]:
    candidates: dict[str, set[str]] = {}
    approved_stores = set()
    for node in nodes:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        candidates.setdefault(target.id, set()).add(value.value)
                        approved_stores.add(id(target))
    invalid = set().union(*(_string_bound_names(node) for node in nodes))
    invalid.update(node.id for node in nodes if isinstance(node, ast.Name)
                   and isinstance(node.ctx, (ast.Store, ast.Del)) and id(node) not in approved_stores)
    return {name: next(iter(values)) for name, values in candidates.items()
            if len(values) == 1 and name not in invalid and "*" not in invalid}


def _env_key(node: ast.AST, origins: dict[str, set[str]]) -> ast.AST | None:
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
        if "mapping" in _value_origins(node.value, origins):
            return node.slice
    elif isinstance(node, ast.Call) and "getter" in _value_origins(node.func, origins):
        if node.args:
            return node.args[0]
        return next((item.value for item in node.keywords
                     if item.arg in {"name", "key"}), ast.Constant(value=None))
    elif isinstance(node, ast.Compare):
        for op, right in zip(node.ops, node.comparators):
            if isinstance(op, (ast.In, ast.NotIn)) and "mapping" in _value_origins(right, origins):
                return node.left
    return None


def scan_reads(source: str) -> list[tuple[int, str | None]]:
    """Find reads via fixed-point provenance; unknown bindings stay unknown."""
    nodes = tuple(ast.walk(ast.parse(source)))
    bindings = _constant_values(nodes)
    origins = {"os": {"os"}, "backend": {"backend"}, "env_config": {"config"},
               "environ": {"mapping"}, "env": {"mapping"}, "getenv": {"getter"},
               "require_env": {"getter"}, "_branded": {"getter"}}
    for node in nodes:
        _import_origins(node, origins)
    # Monotone finite sets: every pass adds provenance or ends the fixed point.
    while _alias_pass(nodes, origins):
        pass
    reads: list[tuple[int, str | None]] = []
    for node in nodes:
        key = _env_key(node, origins)
        value = node.func if isinstance(node, ast.Call) else (
            node.value if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load) else None)
        if value is not None and "unresolved_os_attribute" in _value_origins(value, origins):
            key = ast.Constant(value=None)
        if key is None:
            continue
        name = None
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            name = key.value
        elif isinstance(key, ast.Name):
            name = bindings.get(key.id)
        assert isinstance(node, (ast.Subscript, ast.Call, ast.Compare))
        reads.append((node.lineno, name))
    return sorted(set(reads), key=lambda item: (item[0], item[1] or ""))


def violations(source: str, path: str = "synthetic.py") -> list[str]:
    failures = []
    for line, name in scan_reads(source):
        if name is None:
            if (path, line) not in DYNAMIC_SITES:
                failures.append(f"{path}:{line}: unresolved env name")
        elif not (name.startswith("CLAUDIT_") or name in PLATFORM_NAMES
                  or any(name.startswith(prefix) for prefix in PLATFORM_PREFIXES)):
            failures.append(f"{path}:{line}: unprefixed env name {name}")
    return failures


def test_scanner_finds_and_rejects_unprefixed_reads() -> None:
    source = '\n'.join([
        'import os',
        'from os import environ as settings',
        'from backend.env_config import getenv as config',
        'os.environ["CLAUDIT_KNOWN"]',
        'os.environ.get("BAD_GET")',
        'os.getenv("BAD_GETENV")',
        'environ.get("BAD_ALIAS")',
        'os.environ.setdefault("BAD_DEFAULT", "1")',
        '"BAD_MEMBER" in os.environ',
        'config("BAD_RESOLVER")',
        'settings["BAD_IMPORT"]',
        'os.getenv(dynamic_name)',
        'config(**arguments)',
    ])
    reads = scan_reads(source)
    assert (4, "CLAUDIT_KNOWN") in reads
    assert (5, "BAD_GET") in reads
    failures = violations(source)
    assert len(failures) == 9
    assert "unresolved" in failures[-1]
    assert all("CLAUDIT_KNOWN" not in failure for failure in failures)


def test_backend_and_scripts_use_prefixed_configuration() -> None:
    tracked = subprocess.check_output(
        ["git", "ls-files", "backend", "scripts"], cwd=ROOT, text=True,
    ).splitlines()
    assert "backend/timing.py" in tracked
    assert any(name == "CLAUDIT_TIMING" for _, name in
               scan_reads((ROOT / "backend/timing.py").read_text(encoding="utf-8")))
    failures = []
    observed_dynamic = set()
    for path in tracked:
        source = (ROOT / path).read_text(encoding="utf-8")
        if Path(path).suffix == ".py":
            failures.extend(violations(source, path))
            reads = scan_reads(source)
        elif is_script_module(path, source):
            failures.extend(script_violations(source, path))
            reads = scan_script_reads(source, path)
        else:
            continue
        observed_dynamic.update((path, line) for line, name in reads if name is None)
    assert not failures, '\n'.join(failures)
    assert set(DYNAMIC_SITES) | set(SCRIPT_DYNAMIC_SITES) <= observed_dynamic, "stale dynamic-site exemption"


def _resolver():
    assert (ROOT / "backend/env_config.py").is_file(), "configuration resolver is missing"
    return importlib.import_module("backend.env_config")


@pytest.fixture(autouse=True)
def isolated_fallback_tracking(monkeypatch) -> None:
    monkeypatch.setattr(_resolver(), "_used_deprecated", set())


def _clear_config_env(monkeypatch) -> None:
    for old, new in _resolver().DEPRECATED_ENV_FALLBACKS.items():
        monkeypatch.delenv(old, raising=False)
        monkeypatch.delenv(new, raising=False)


def test_fallback_map_is_prefixed_and_only_released_names() -> None:
    config = _resolver()
    assert config.DEPRECATED_ENV_FALLBACKS["DATABASE_URL_VIZ"] == "CLAUDIT_DATABASE_URL_VIZ"
    assert all(new == "CLAUDIT_" + old
               for old, new in config.DEPRECATED_ENV_FALLBACKS.items())
    assert "GUEST_LOGIN" not in config.DEPRECATED_ENV_FALLBACKS
    assert "R2_BLOB_CACHE" not in config.DEPRECATED_ENV_FALLBACKS


@pytest.mark.parametrize("legacy, preferred", [
    ("DATABASE_URL_VIZ", "new-value"),
    ("R2_BUCKET", "new-value"),
    ("APP_NAME", "new-value"),
    ("APP_NAME", ""),
], ids=["database", "bucket", "brand", "empty-brand"])
def test_released_fallback_precedence_and_warning(legacy, preferred, monkeypatch, caplog) -> None:
    config = _resolver()
    name = "CLAUDIT_" + legacy
    _clear_config_env(monkeypatch)
    log = logging.getLogger("claudit.env-test")
    caplog.set_level(logging.WARNING, logger=log.name)
    assert config.getenv(name, "default") == "default"
    if preferred == "":
        monkeypatch.setenv(legacy, "old-value")
        assert config.getenv(name) == "old-value"
    monkeypatch.setenv(name, preferred)
    assert config.getenv(name, "default") == preferred
    monkeypatch.setenv(legacy, "old-value")
    assert config.getenv(name, "default") == preferred
    config.warn_deprecated_env(log)
    assert not caplog.records
    monkeypatch.delenv(name)
    assert config.getenv(name) == "old-value"
    assert config.getenv(name) == "old-value"
    config.warn_deprecated_env(log)
    assert len(caplog.records) == 1
    assert legacy in caplog.text and name in caplog.text
    assert "old-value" not in caplog.text and "new-value" not in caplog.text


def test_unreleased_guest_login_has_no_fallback(monkeypatch) -> None:
    config = _resolver()
    monkeypatch.delenv("CLAUDIT_GUEST_LOGIN", raising=False)
    monkeypatch.setenv("GUEST_LOGIN", "0")
    assert config.getenv("CLAUDIT_GUEST_LOGIN", "default") == "default"
    assert guest_login.guest_login_enabled()
    monkeypatch.setenv("CLAUDIT_GUEST_LOGIN", "0")
    assert config.getenv("CLAUDIT_GUEST_LOGIN") == "0"
    assert not guest_login.guest_login_enabled()
    monkeypatch.setenv("CLAUDIT_GUEST_LOGIN", "1")
    assert guest_login.guest_login_enabled()
    monkeypatch.delenv("GUEST_LOGIN")
    assert config.getenv("CLAUDIT_GUEST_LOGIN") == "1"
    monkeypatch.delenv("CLAUDIT_GUEST_LOGIN")
    assert config.getenv("CLAUDIT_GUEST_LOGIN", "default") == "default"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["DATABASE_URL_VIZ", "CLAUDIT_DATABASE_URL_VIZ"])
async def test_export_child_resolves_inherited_database(name, monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("DATABASE_URL_VIZ", raising=False)
    monkeypatch.delenv("CLAUDIT_DATABASE_URL_VIZ", raising=False)
    monkeypatch.setenv(name, "postgresql:///export-fixture")
    out = tmp_path / "resolved.txt"
    program = (
        "import sys; from pathlib import Path; "
        "sys.path.insert(0, 'scripts/plots'); "
        "from ccusage_plot_db import _resolve_db_url; "
        "Path(sys.argv[1]).write_text(_resolve_db_url(None))"
    )
    await api_export._render_export(  # pylint: disable=protected-access
        [sys.executable, "-c", program, str(out)], str(out))
    assert out.read_text() == "postgresql:///export-fixture"


def test_dotenv_keeps_released_names_working(monkeypatch, tmp_path, caplog) -> None:
    config = _resolver()
    monkeypatch.delenv("CLAUDIT_APP_NAME", raising=False)
    monkeypatch.delenv("APP_NAME", raising=False)
    # Register the initially absent key with monkeypatch before dotenv adds it.
    monkeypatch.setenv("APP_NAME", "temporary")
    monkeypatch.delenv("APP_NAME")
    dotenv = tmp_path / "legacy.env"
    dotenv.write_text("APP_NAME=legacy-brand\n")
    db.load_dotenv(str(dotenv))
    assert branding.brand_name() == "legacy-brand"
    config.warn_deprecated_env(logging.getLogger("claudit.env-test"))
    assert "APP_NAME" in caplog.text
    monkeypatch.setenv("CLAUDIT_APP_NAME", "preferred-brand")
    assert branding.brand_name() == "preferred-brand"


def test_required_setting_preserves_missing_error(monkeypatch) -> None:
    config = _resolver()
    monkeypatch.delenv("DATABASE_URL_VIZ", raising=False)
    monkeypatch.delenv("CLAUDIT_DATABASE_URL_VIZ", raising=False)
    with pytest.raises(KeyError, match="CLAUDIT_DATABASE_URL_VIZ"):
        config.require_env("CLAUDIT_DATABASE_URL_VIZ")
    monkeypatch.setenv("CLAUDIT_DATABASE_URL_VIZ", "")
    assert config.require_env("CLAUDIT_DATABASE_URL_VIZ") == ""


def test_resolver_rejects_unprefixed_api(monkeypatch) -> None:
    config = _resolver()
    monkeypatch.setenv("CLAUDIT_APP_NAME", "brand")
    assert config.getenv("CLAUDIT_APP_NAME") == "brand"
    with pytest.raises(ValueError, match="CLAUDIT_"):
        config.getenv("APP_NAME")


def test_dynamic_registry_names_are_prefixed() -> None:
    names = [spec.env for spec in (*COLLECTION_SPECS, *FEATURE_SPECS)]
    names.append(flags.LEGACY_HIDE_RATE_LIMITS_ENV)
    assert "CLAUDIT_COLLECT_TURN_CONTEXT" in names
    assert all(name.startswith("CLAUDIT_") for name in names)


@pytest.mark.asyncio
async def test_startup_reports_late_read_legacy_setting(monkeypatch, caplog) -> None:
    monkeypatch.delenv("CLAUDIT_COOKIE_SECURE", raising=False)
    monkeypatch.setenv("COOKIE_SECURE", "0")

    def stop_before_database() -> None:
        raise RuntimeError("startup observation complete")

    monkeypatch.setattr(app.db, "apply_schema", stop_before_database)
    with pytest.raises(RuntimeError, match="startup observation complete"):
        async with app.lifespan(app.app):
            pass
    assert sum("COOKIE_SECURE" in record.message for record in caplog.records) == 1
    caplog.clear()
    monkeypatch.setenv("CLAUDIT_COOKIE_SECURE", "1")
    with pytest.raises(RuntimeError, match="startup observation complete"):
        async with app.lifespan(app.app):
            pass
    assert not caplog.records


@pytest.mark.parametrize("prefix", ["", "CLAUDIT_"])
def test_storage_client_uses_released_credentials(prefix, monkeypatch) -> None:
    monkeypatch.setattr(r2, "_tls", threading.local())
    for suffix in ["R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"]:
        monkeypatch.delenv("CLAUDIT_" + suffix, raising=False)
        monkeypatch.delenv(suffix, raising=False)
    monkeypatch.setenv(prefix + "R2_ENDPOINT", "https://r2.invalid/s3")
    # Credentials are synthesized at runtime; the client makes no requests.
    monkeypatch.setenv(prefix + "R2_ACCESS_KEY_ID", "fixture-" + str(id(monkeypatch)))
    monkeypatch.setenv(prefix + "R2_SECRET_ACCESS_KEY", "fixture-" + str(id(prefix)))
    client = r2._boto_client()  # pylint: disable=protected-access
    assert client.meta.endpoint_url == "https://r2.invalid/s3"
    client.close()


@pytest.mark.parametrize("name", ["DATABASE_URL_VIZ", "CLAUDIT_DATABASE_URL_VIZ"])
def test_plot_cli_reads_dotenv_and_reports_legacy(name, monkeypatch, tmp_path, caplog) -> None:
    _clear_config_env(monkeypatch)
    monkeypatch.setenv(name, "temporary")
    monkeypatch.delenv(name)
    spec = importlib.util.spec_from_file_location(
        "env_prefix_plot", ROOT / "scripts/plots/ccusage_plot_db.py")
    assert spec and spec.loader
    plot = importlib.util.module_from_spec(spec)
    monkeypatch.syspath_prepend(str(ROOT / "scripts/plots"))
    spec.loader.exec_module(plot)
    dotenv = tmp_path / "plot.env"
    dotenv.write_text(name + "='postgresql:///plot-fixture'\n")
    monkeypatch.setattr(plot, "DEFAULT_DOTENV", dotenv)
    monkeypatch.setattr(sys, "argv", ["plot", "--all"])
    monkeypatch.setattr(plot, "load_events", lambda *_args, **_kwargs: [])
    with pytest.raises(SystemExit) as stopped:
        plot.main()
    assert stopped.value.code == 3
    assert plot.DB_URL == "postgresql:///plot-fixture"
    if name == "DATABASE_URL_VIZ":
        assert len(caplog.records) == 1 and "DATABASE_URL_VIZ" in caplog.text
    else:
        assert not caplog.records


def test_warning_records_fallback_used_before_startup(monkeypatch, caplog) -> None:
    config = _resolver()
    _clear_config_env(monkeypatch)
    monkeypatch.setenv("EXPORT_PYTHON", "legacy-python")
    assert config.getenv("CLAUDIT_EXPORT_PYTHON") == "legacy-python"
    monkeypatch.delenv("EXPORT_PYTHON")
    config.warn_deprecated_env(logging.getLogger("claudit.env-test"))
    assert len(caplog.records) == 1 and "EXPORT_PYTHON" in caplog.text
    caplog.clear()
    config.warn_deprecated_env(logging.getLogger("claudit.env-test"))
    assert not caplog.records


def test_scanner_rejects_ambiguous_or_shadowed_names() -> None:
    source = '\n'.join([
        'import os',
        'NAME = "BAD_NAME"',
        'os.getenv(NAME)',
        'NAME = "CLAUDIT_LATER"',
        'os.getenv(NAME)',
    ])
    assert violations(source), "a later assignment hid an unprefixed read"
    shadowed = '\n'.join([
        'import os',
        'SETTING = "CLAUDIT_GLOBAL"',
        'def read(SETTING):',
        '    return os.getenv(SETTING)',
    ])
    assert violations(shadowed), "a parameter was mistaken for the global constant"
    constant = 'import os\nSETTING = "CLAUDIT_GLOBAL"\nos.getenv(SETTING)'
    assert scan_reads(constant) == [(3, "CLAUDIT_GLOBAL")]
    assert not violations(constant)
    module_call = 'import backend.env_config\nbackend.env_config.getenv("BAD_MODULE")'
    assert violations(module_call), "a module-qualified resolver escaped the scan"


@pytest.mark.parametrize("getter", [
    "os.getenv", "os.environ.get", "os.environ.setdefault", "getenv",
    "source_os.getenv", "source_os.environ.get", "mapping.get",
])
def test_scanner_getter_alias_reads(getter) -> None:
    template = ("import os\nfrom backend.env_config import getenv\n"
                "source_os = os\nmapping = source_os.environ\n"
                f"review_read_env = {getter}\n"
                "review_read_env({name!r}, 'default')")
    good = template.format(name="CLAUDIT_REVIEW_SETTING")
    bad = template.format(name="UNPREFIXED_REVIEW_SETTING")
    assert violations(bad), "getter alias hid the unprefixed read"
    assert any(name == "CLAUDIT_REVIEW_SETTING" for _, name in scan_reads(good))
    assert not violations(good)


def test_scanner_alias_fixpoint() -> None:
    template = ("import os\ndef lookup():\n"
                "    alias_three = alias_two\n"
                "    alias_two = alias_one\n"
                "    alias_one = os.getenv\n"
                "    return alias_three({name!r})")
    good = template.format(name="CLAUDIT_REVIEW_SETTING")
    assert scan_reads(good) == [(6, "CLAUDIT_REVIEW_SETTING")]
    assert not violations(good)
    assert violations(template.format(name="UNPREFIXED_REVIEW_SETTING"))


@pytest.mark.parametrize("read", [
    'getattr(os, "getenv")({name!r}, "default")',
    'getattr(source_os, "getenv")({name!r}, "default")',
    'getattr(os, "environ")[{name!r}]',
    'getattr(source_os, "environ").get({name!r})',
    'reader = getattr(source_os, "getenv")\nreader({name!r})',
    'mapping = getattr(source_os, "environ")\nmapping[{name!r}]',
])
def test_scanner_reflective_env_reads(read) -> None:
    template = 'import os as source_os\nimport os\n' + read
    good = template.format(name="CLAUDIT_REVIEW_SETTING")
    bad = template.format(name="UNPREFIXED_SETTING")
    assert any("unprefixed env name UNPREFIXED_SETTING" in failure
               for failure in violations(bad))
    assert any(name == "CLAUDIT_REVIEW_SETTING" for _, name in scan_reads(good))
    assert not violations(good)


@pytest.mark.parametrize("read, attribute", [
    ('getattr(os, attribute)("CLAUDIT_REVIEW_SETTING")', 'getenv'),
    ('getattr(source_os, attribute)["CLAUDIT_REVIEW_SETTING"]', 'environ'),
    ('reader = getattr(source_os, attribute)\nreader("CLAUDIT_REVIEW_SETTING")', 'getenv'),
    ('mapping = getattr(os, attribute)\nmapping["CLAUDIT_REVIEW_SETTING"]', 'environ'),
])
@pytest.mark.parametrize("binding", ['', 'attribute = "getenv"\n'])
def test_scanner_rejects_unresolved_reflective_reads(read, attribute, binding) -> None:
    template = 'import os as source_os\nimport os\n' + read
    good = template.replace('attribute', repr(attribute))
    assert any(name == "CLAUDIT_REVIEW_SETTING" for _, name in scan_reads(good))
    assert not violations(good)
    assert any("unresolved env name" in failure for failure in violations(binding + template))


@pytest.mark.parametrize("binding", [
    'for NAME in ["UNPREFIXED_REVIEW_SETTING"]: pass',
    'async def bind():\n    async for NAME in stream: pass',
    'with manager as NAME: pass',
    'async def bind():\n    async with manager as NAME: pass',
    'try: pass\nexcept Exception as NAME: pass',
    'try: pass\nexcept* Exception as NAME: pass',
    '(NAME := "UNPREFIXED_REVIEW_SETTING")',
    'NAME += "suffix"',
    'import other as NAME',
    'from other import setting as NAME',
    'def bind():\n    global NAME',
    'def bind():\n    nonlocal NAME',
    'match value:\n    case {"key": NAME}: pass',
    'match value:\n    case [*NAME]: pass',
    'match value:\n    case {**NAME}: pass',
    '[NAME for NAME in values]',
    '{NAME for NAME in values}',
    '{NAME: NAME for NAME in values}',
    '(NAME for NAME in values)',
    'del NAME',
    'NAME, other = values',
    'def NAME(): pass',
    'class NAME: pass',
])
def test_scanner_invalidates_unresolved_binding_forms(binding) -> None:
    template = 'import os\nNAME = "CLAUDIT_KNOWN"\n' + binding + '\nos.getenv(READ_MARKER)'
    # A literal control proves the read is found even in these binding scopes.
    good = template.replace('READ_MARKER', '"CLAUDIT_REVIEW_SETTING"')
    assert any(name == "CLAUDIT_REVIEW_SETTING" for _, name in scan_reads(good))
    assert not violations(good)
    assert violations(template.replace("READ_MARKER", "NAME")), "binding reused an obsolete constant"


def _compose_config(tmp_path, supplied, source):
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker compose binary is absent")
    version = subprocess.run([docker, "compose", "version"], capture_output=True, text=True, check=False)
    if version.returncode and "is not a docker command" in version.stderr:
        pytest.skip("docker compose binary is absent")
    assert version.returncode == 0, version.stderr
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("CLAUDIT_") and key not in _resolver().DEPRECATED_ENV_FALLBACKS}
    dotenv = tmp_path / "compose.env"
    dotenv.write_text('\n'.join(f"{key}={value}" for key, value in supplied.items()) if source == "dotenv" else "")
    if source == "process":
        environment.update(supplied)
    result = subprocess.run([docker, "compose", "--env-file", str(dotenv), "config", "--format", "json"],
                            cwd=ROOT, env=environment, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["services"]["app"]["environment"]


@pytest.mark.parametrize("source", ["process", "dotenv"])
@pytest.mark.parametrize("case", ["old", "new", "both", "neither", "empty_preferred"])
def test_compose_resolved_migration_values(source, case, tmp_path) -> None:
    user = "postgres"
    defaults = {
        "CLAUDIT_DATABASE_URL_VIZ": f"postgresql://{user}:{user}@db:5432/claudit",
        "CLAUDIT_DATABASE_URL_AUTH": f"postgresql://{user}:{user}@db:5432/claudit_auth",
        "CLAUDIT_R2_ENDPOINT": "file:///opt/claudit/fixtures/r2_mini/",
        "CLAUDIT_R2_BUCKET": "claude",
        "CLAUDIT_R2_ACCESS_KEY_ID": "",
        "CLAUDIT_R2_SECRET_ACCESS_KEY": "",
    }
    supplied = {}
    expected = {}
    for old, new in _resolver().DEPRECATED_ENV_FALLBACKS.items():
        if new not in defaults:
            continue
        old_value, new_value = "legacy-" + old.lower(), "preferred-" + old.lower()
        if case in {"old", "both", "empty_preferred"}:
            supplied[old] = old_value
        if case in {"new", "both", "empty_preferred"}:
            supplied[new] = "" if case == "empty_preferred" else new_value
        expected[new] = ("" if case == "empty_preferred" else new_value
                         if case in {"new", "both"} else old_value if case == "old" else defaults[new])
    resolved = _compose_config(tmp_path, supplied, source)
    assert "CLAUDIT_DATABASE_URL_VIZ" in expected and "CLAUDIT_R2_ENDPOINT" in expected
    assert all(resolved[key] == value for key, value in expected.items())


@pytest.mark.parametrize("old_env, file_key", [
    ("", "DATABASE_URL_VIZ"),
    ("postgresql:///old-export", "CLAUDIT_DATABASE_URL_VIZ"),
])
def test_plot_cli_mixed_source_migration(old_env, file_key, monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("CLAUDIT_DATABASE_URL_VIZ", raising=False)
    monkeypatch.setenv("DATABASE_URL_VIZ", old_env)
    monkeypatch.syspath_prepend(str(ROOT / "scripts/plots"))
    plot = importlib.import_module("ccusage_plot_db")
    dotenv = tmp_path / "plot.env"
    dotenv.write_text(file_key + "='postgresql:///dotenv-fixture'\n")
    monkeypatch.setattr(plot, "DEFAULT_DOTENV", dotenv)
    assert plot._resolve_db_url("postgresql:///cli-fixture") == "postgresql:///cli-fixture"  # pylint: disable=protected-access
    assert plot._resolve_db_url(None) == "postgresql:///dotenv-fixture"  # pylint: disable=protected-access
    monkeypatch.setenv("CLAUDIT_DATABASE_URL_VIZ", "postgresql:///new-export")
    assert plot._resolve_db_url(None) == "postgresql:///new-export"  # pylint: disable=protected-access
