"""src/ctx-input.js: the peak-of-iterations rule the Inspector prices by.

Mirrors backend/ctx_input.py, which reads its iteration list through
json_shape.dict_list: only OBJECT entries count, so a malformed entry
(null, array, scalar) is skipped rather than thrown on.
What survives the filter then decides between the >1-iteration peak and
the top-level fresh+create+read fallback.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

CTX_INPUT_JS = Path(__file__).resolve().parents[1] / "src" / "ctx-input.js"


def _run(body: str) -> dict:
    script = f"""
      global.window = {{}};
      require({str(CTX_INPUT_JS)!r});
      {body}
    """
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_malformed_iteration_entries_are_skipped():
    """Each non-object shape is dropped before the >1 peak test, so the
    record falls back to its top-level sum rather than throwing or
    reading the non-object's absent fields as zero."""
    out = _run("""
      const base = {
        input_tokens: 11, cache_creation_input_tokens: 7,
        cache_read_input_tokens: 3, output_tokens: 5,
      };
      const call = (iterations) =>
        window.usageCtxInput(Object.assign({}, base, { iterations }));
      console.log(JSON.stringify({
        nullEntry: call([null, { input_tokens: 90 }]),
        arrayEntry: call([[1, 2], [3, 4]]),
        scalarEntry: call([5, 'x']),
        mixed: call([null, [1, 2], 5, { input_tokens: 90 }]),
      }));
    """)
    assert out == {
        # One surviving object iteration -> top-level sum 11 + 7 + 3.
        "nullEntry": 21,
        # No surviving object entry -> the fallback sum, never the
        # non-object's absent fields read as zero.
        "arrayEntry": 21,
        "scalarEntry": 21,
        "mixed": 21,
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_peak_rule_and_single_iteration_fallback_preserved():
    """The filter must not disturb the >1-iteration peak or the
    single/absent fallbacks it guards.

    `peakWithMalformed` is the branch case: the peak loop must run over
    the FILTERED list, so a malformed entry beside two valid iterations
    is dropped rather than read. Iterating the raw array there would
    throw on the null.
    """
    out = _run("""
      const call = (u) => window.usageCtxInput(u);
      console.log(JSON.stringify({
        peak: call({ input_tokens: 11, iterations: [
          { input_tokens: 50, cache_read_input_tokens: 10 },
          { input_tokens: 90, cache_read_input_tokens: 30 },
        ]}),
        peakWithMalformed: call({ input_tokens: 11, iterations: [
          null,
          { input_tokens: 50, cache_read_input_tokens: 10 },
          { input_tokens: 90, cache_read_input_tokens: 30 },
        ]}),
        singleIter: call({ input_tokens: 11, cache_creation_input_tokens: 7,
          cache_read_input_tokens: 3, iterations: [{ input_tokens: 90 }] }),
        noIterations: call({ input_tokens: 11,
          cache_creation_input_tokens: 7, cache_read_input_tokens: 3 }),
        absent: call(null),
      }));
    """)
    assert out == {
        # max(50+10, 90+30) = 120, not the 161 billing sum.
        "peak": 120,
        # The malformed entry is dropped BEFORE the peak loop, so the two
        # valid iterations still peak at 120.
        "peakWithMalformed": 120,
        # One iteration -> top-level sum, not the iteration's own tokens.
        "singleIter": 21,
        "noIterations": 21,
        "absent": 0,
    }
