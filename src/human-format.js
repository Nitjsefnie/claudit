// 3 significant digits; half-up away from zero; unit after rounding; precision degrades above 2^53.
// Pinned by tests/test_human_format_js.py.
(function () {
  const UNITS = ['', 'K', 'M', 'B', 'T'];

  function fmt(v, isCurrency) {
    if (!Number.isFinite(v)) return String(v);
    const prefix = (isCurrency ? '$' : '') + (v < 0 ? '-' : '');
    const abs = Math.abs(v);
    if (abs === 0) return isCurrency ? '$0.00' : '0';
    if (abs < 1000 && !isCurrency && Number.isInteger(v)) return String(v);

    const [mantissa, power = '0'] = String(abs).split('e');
    const raw = mantissa.replace('.', '');
    const leading = raw.search(/[1-9]/);
    const decimal = mantissa.includes('.') ? mantissa.indexOf('.') : mantissa.length;
    let exponent = Number(power) + decimal - leading - 1;
    const significant = raw.slice(leading);
    let digits = significant.slice(0, 3).padEnd(3, '0');
    if (significant[3] >= '5') {
      let i = 2;
      while (i >= 0 && digits[i] === '9') i -= 1;
      if (i < 0) {
        digits = '100';
        exponent += 1;
      } else {
        digits = digits.slice(0, i) + String.fromCharCode(digits.charCodeAt(i) + 1)
          + '0'.repeat(2 - i);
      }
    }
    const unit = Math.min(4, Math.max(0, Math.floor(exponent / 3)));
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
