"""One compiler-code/container traversal for both scan-gate vocabularies."""
from __future__ import annotations

from dataclasses import dataclass
import types

from tests.source_scan_cache import for_source


@dataclass(frozen=True)
class _Index:
    vocab: tuple[frozenset[str], frozenset[str]]
    def_vocab: tuple[frozenset[str], bool]


_indexes: dict[str, _Index] = {}


def _build(code: types.CodeType) -> _Index:
    # 3.15 loads '' (and None) via LOAD_COMMON_CONSTANT, so '' can sit
    # out co_consts; admitting it here is the fail-closed direction.
    consts: set[str] = {''}
    used: set[str] = set()
    defined: set[str] = set()
    stack: list[object] = [code]
    while stack:
        item = stack.pop()
        if isinstance(item, types.CodeType):
            used.update(item.co_names)
            defined.update(item.co_names)
            defined.update(item.co_varnames)
            defined.add(item.co_name)
            stack.extend(item.co_consts)
        elif isinstance(item, str):
            consts.add(item)
        elif isinstance(item, (tuple, frozenset, set, list)):
            stack.extend(item)
    defined.update(consts)
    return _Index((frozenset(consts), frozenset(used)),
                  (frozenset(defined),
                   "pytest" in defined and "fixture" in defined))


def _index(source: str) -> _Index:
    result = _indexes.get(source)
    if result is None:
        result = _build(for_source(source).scan_code())
        _indexes[source] = result
    return result


def vocab(source: str) -> tuple[frozenset[str], frozenset[str]]:
    return _index(source).vocab


def def_vocab(source: str) -> tuple[frozenset[str], bool]:
    return _index(source).def_vocab
