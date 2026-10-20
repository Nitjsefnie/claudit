#!/usr/bin/env python3
"""Validate and track a vendor namespace before the ordinary pricing refresh."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
# pylint: disable=wrong-import-position
from refresh_common import detection_stamp  # noqa: E402
from refresh_pricelog import (  # noqa: E402
    FetchModels, _catalog_slugs, fetch_models as _fetch_models)
from refresh_prices import RefreshError  # noqa: E402
from backend import pricing  # noqa: E402
from backend.pricing_document import (  # noqa: E402
    expand_pricing_doc, serialize_pricing_doc)

PRICING_JSON = REPO_ROOT / "src" / "pricing.json"


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Track an OpenRouter vendor namespace for the pricing refresh.")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--pricing-path", type=Path)
    parser.add_argument("--commit-msg", type=Path,
                        help="write the prefix edit's commit message here")
    return parser.parse_args(argv)


def _require_present(namespace: str, fetch_models: FetchModels) -> None:
    try:
        ids, reason = _catalog_slugs(fetch_models())
    except Exception as exc:
        raise RefreshError(f"catalog is unreadable: {exc}") from exc
    if reason:
        raise RefreshError(f"catalog is unreadable: {reason}")
    if not any(model_id.partition("/")[0] == namespace for model_id in ids):
        raise RefreshError("namespace is absent from OpenRouter's catalog")


def _write_pricing(path: Path, serialized: str) -> None:
    with TemporaryDirectory(dir=path.parent) as scratch:
        staged = Path(scratch) / path.name
        staged.write_text(serialized, encoding="utf-8", newline="\n")
        if staged.read_bytes() != serialized.encode("utf-8"):
            raise RefreshError("pricing write did not preserve the serialized bytes")
        shutil.copymode(path, staged)
        staged.replace(path)


def main(argv: list[str] | None = None, *,
         fetch_models: FetchModels = _fetch_models,
         pricing_path: Path = PRICING_JSON,
         now: datetime | None = None) -> int:
    args = _arguments(argv)
    namespace = args.namespace
    pricing_path = args.pricing_path or pricing_path
    try:
        if not re.fullmatch(r"[a-z0-9-]+", namespace):
            raise RefreshError("malformed namespace; expected lowercase [a-z0-9-]+")
        raw = json.loads(pricing_path.read_text(encoding="utf-8"))
        prefixes = raw["openrouter"]["vendor"]["prefixes"]
        if namespace in prefixes:
            raise RefreshError("namespace is already tracked")
        _require_present(namespace, fetch_models)
        prefixes.append(namespace)
        try:
            doc = expand_pricing_doc(raw)
            pricing.load_tables(doc)
        except ValueError as exc:
            raise RefreshError(f"the updated file would not load: {exc}") from exc
        serialized = serialize_pricing_doc(doc)
        _write_pricing(pricing_path, serialized)
        stamp = detection_stamp(now or datetime.now(timezone.utc))
        body = (f"Track {namespace!r} in openrouter.vendor.prefixes, validated "
                f"against OpenRouter's catalog at {stamp}.")
        if args.commit_msg:
            args.commit_msg.write_text(
                f"Track OpenRouter vendor namespace {namespace}\n\n{body}\n\n"
                "Captured by .github/workflows/refresh-pricing.yml.\n",
                encoding="utf-8", newline="\n")
    except (RefreshError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"track_vendor: {namespace!r}: {exc}", file=sys.stderr)
        return 1
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
