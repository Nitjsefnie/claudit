#!/usr/bin/env python3
"""Convert pricing.json to its canonical omitted-default rate spelling."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
# pylint: disable=wrong-import-position
from backend import pricing
from backend.pricing_document import serialize_pricing_doc

PRICING_JSON = REPO_ROOT / "src" / "pricing.json"


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Omit cache-write rates equal to fresh in pricing.json.")
    parser.add_argument("--pricing", type=Path, default=PRICING_JSON,
                        help="pricing document to convert")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    try:
        doc = json.loads(args.pricing.read_text(encoding="utf-8"))
        pricing.load_tables(doc)
        converted = serialize_pricing_doc(doc)
        args.pricing.write_text(converted, encoding="utf-8")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"convert_pricing_doc: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
