"""Synthetic catalog and endpoint helpers for vendor refresh tests."""
from __future__ import annotations

import copy
import importlib.util
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from backend import long_context
from tests.refresh_fixture_builders import seed_doc

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
NOW = datetime(2031, 1, 1, tzinfo=UTC)
STAMP = "2031-01-01T00:00:00Z"
RATE_FIELDS = ("fresh", "create_5m", "create_1h", "read", "output")
GPT_ID = "openai/gpt-test-9.9"
GPT_KEY = "gpt-test-9-9"
GLM_ID = "z-ai/glm-test-1"
GLM_KEY = "glm-test-1"
RATES = {"fresh": 1.0, "create_5m": 1.25, "create_1h": 2.0, "read": 0.1,
         "output": 5.0}
TRACKED = {"id": GPT_ID, "vendor_host": "Vendor"}


def _load(name: str):
    path = ROOT / "scripts" / "ci" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


vendor = _load("refresh_vendor_rates")


def _per_token(rate: float) -> str:
    return format(Decimal(repr(rate)).scaleb(-6).normalize(), "f")


def _doc(*, members=None, models=None, resolve=None, tracked=None,
         meters=None, prefixes=None) -> dict:
    return seed_doc(members=members, models=models, resolve=resolve,
                    tracked=tracked, meters=meters, prefixes=prefixes,
                    fetched="2030-12-31T00:00:00Z")


def _price(fresh, output, read=None, write=None, write_1h=None, **extra) -> dict:
    price = {"prompt": _per_token(fresh), "completion": _per_token(output)}
    if read is not None:
        price["input_cache_read"] = _per_token(read)
    if write is not None:
        price["input_cache_write"] = _per_token(write)
    if write_1h is not None:
        price["input_cache_write_1h"] = _per_token(write_1h)
    price.update(extra)
    return price


def _endpoint(tag: str, price: dict, host: str = "Vendor") -> dict:
    return {"provider_name": host, "tag": tag, "quantization": "fp8",
            "status": 0, "context_length": 131072, "pricing": price}


def _payload(*endpoints: dict) -> dict:
    return {"data": {"endpoints": list(endpoints)}}


def _catalog(*ids: str) -> dict:
    return {"data": [{"id": i, "architecture": {
        "output_modalities": ["text"]}} for i in ids]}


def _band(fresh: float, output: float, read=None, write=None, write_1h=None, *,
          threshold=None, input_mult=None, output_mult=None) -> dict:
    tin = long_context.LONG_CONTEXT_INPUT_MULT if input_mult is None else input_mult
    tout = (long_context.LONG_CONTEXT_OUTPUT_MULT if output_mult is None
            else output_mult)
    band = {"min_prompt_tokens": (long_context.LONG_CONTEXT_THRESHOLD
                                  if threshold is None else threshold),
            "prompt": _per_token(fresh * tin),
            "completion": _per_token(output * tout)}
    if read is not None:
        band["input_cache_read"] = _per_token(read * tin)
    if write is not None:
        band["input_cache_write"] = _per_token(write * tin)
    if write_1h is not None:
        band["input_cache_write_1h"] = _per_token(write_1h * tin)
    return band


def _move_meter(threshold: int, input_mult: float = long_context.LONG_CONTEXT_INPUT_MULT,
                output_mult: float = long_context.LONG_CONTEXT_OUTPUT_MULT) -> dict:
    return {"threshold": threshold, "input_mult": input_mult,
            "output_mult": output_mult}


def _run(doc: dict, catalog: dict, endpoints: dict):
    doc = copy.deepcopy(doc)
    outcome = vendor.vendor_pass(doc, lambda: catalog,
                                 lambda mid: endpoints[mid])
    return doc, outcome
