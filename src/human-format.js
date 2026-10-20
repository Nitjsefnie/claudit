// 3 significant digits; half-up away from zero; keep trailing zeros; choose unit after rounding.
// Pinned by tests/test_human_format_js.py.
(function () {
  const UNITS = ['', 'K', 'M', 'B', 'T'];

  function fmt(v, isCurrency) {
    if (!Number.isFinite(v)) return String(v);
    const prefix = (isCurrency ? '$' : '') + (v < 0 ? '-' : '');
    const abs = Math.abs(v);
    if (abs === 0) return isCurrency ? '$0.00' : '0';
    if (abs < 1000 && !isCurrency && Number.isInteger(v)) return String(v);

    const [mantissa, power] = abs.toExponential().split('e');
    let exponent = Number(power);
    let rounded = Math.round(abs >= 1000
      ? abs / (10 ** (exponent - 2))
      : Number(mantissa + 'e2'));
    if (rounded === 1000) {
      rounded = 100;
      exponent += 1;
    }
    const unit = Math.min(4, Math.max(0, Math.floor(exponent / 3)));
    const digits = String(rounded);
    const point = exponent - unit * 3 + 1;
    let out;
    if (point >= digits.length) out = digits + '0'.repeat(point - digits.length);
    else if (point <= 0) out = '0.' + '0'.repeat(-point) + digits;
    else out = digits.slice(0, point) + '.' + digits.slice(point);
    return prefix + out + UNITS[unit];
  }

  function currency(v) { return fmt(v, true); }

  window.humanFormat = { fmt, currency };
})();
