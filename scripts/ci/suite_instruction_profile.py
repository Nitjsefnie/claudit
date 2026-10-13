from __future__ import annotations

import json
from pathlib import Path
from types import CodeType


class InstructionProfile:
    def __init__(self, destination: str):
        self.destination = Path(destination)
        self.counts: dict[int, dict[str, int]] = {}
        self.codes: dict[int, CodeType] = {}

    def count(self, code: CodeType, phase: str) -> None:
        key = id(code)
        counts = self.counts.get(key)
        if counts is None:
            self.codes[key] = code
            counts = self.counts[key] = {'collection': 0, 'run': 0, 'residual': 0}
        counts[phase] += 1

    def write(self) -> None:
        rows = [{'file': self.codes[key].co_filename, 'function': self.codes[key].co_qualname,
                 'line': self.codes[key].co_firstlineno, 'counts': counts}
                for key, counts in self.counts.items()]
        self.destination.write_text(json.dumps(rows, indent=2) + '\n', encoding='utf-8')
