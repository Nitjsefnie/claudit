// The pricing.json schedule spelling scanner (SV-RATE-DATA): a number
// under a schedule window's start or end must be spelled a plain JSON
// integer: Python reads 1400.0 as a float and refuses it (pricing._hhmm),
// while JSON.parse reads it as 1400 and would silently price what Python
// refuses to load. No reviver can catch this — it only ever has
// source-text access under node, so the browser silently accepted 1400.0
// — so both load paths run this check on the RAW text, before parsing.
// The check is scoped by structure, not the key name: only a start/end
// that is a member of an object which is a direct ELEMENT of the array
// that is the value of a "schedule" key is an HHMM position; a fractional
// start anywhere else (a future openrouter.start, a window's rates
// object) parses untouched. A shape the scan cannot spell-check past (a
// leading zero, a bare minus) bails it silently and JSON.parse names
// that shape.
(function () {
  const _pricingError = (detail) => new Error(`pricing.json: ${detail}`);

  const checkHhmmSpelling = (text) => {
    const offenses = [];
    let pos = 0;
    const n = text.length;
    const stack = [];
    const top = () => stack[stack.length - 1];

    const skipWs = () => {
      while (pos < n && ' \t\n\r'.includes(text[pos])) pos++;
    };

    const readString = () => {
      let j = pos + 1;
      let out = '';
      while (j < n) {
        const c = text[j];
        if (c === '"') { pos = j + 1; return out; }
        if (c === '\\') {
          const e = text[j + 1];
          if (e === 'u') {
            const hex = text.slice(j + 2, j + 6);
            if (!/^[0-9a-fA-F]{4}$/.test(hex)) return null;
            out += String.fromCharCode(parseInt(hex, 16));
            j += 6;
          } else {
            const esc = { '"': '"', '\\': '\\', '/': '/', b: '\b', f: '\f',
                          n: '\n', r: '\r', t: '\t' }[e];
            if (esc === undefined) return null;
            out += esc;
            j += 2;
          }
        } else if (c < ' ') {
          return null;
        } else {
          out += c;
          j++;
        }
      }
      return null;
    };

    const readNumber = () => {
      const start = pos;
      if (text[pos] === '-') pos++;
      while (pos < n && text[pos] >= '0' && text[pos] <= '9') pos++;
      if (text[pos] === '.') {
        pos++;
        while (pos < n && text[pos] >= '0' && text[pos] <= '9') pos++;
      }
      if (text[pos] === 'e' || text[pos] === 'E') {
        pos++;
        if (text[pos] === '+' || text[pos] === '-') pos++;
        while (pos < n && text[pos] >= '0' && text[pos] <= '9') pos++;
      }
      return text.slice(start, pos);
    };

    let dead = false;
    const finishValue = () => {
      for (;;) {
        skipWs();
        const t = top();
        if (!t) { dead = true; return; }
        if (!t.obj) t.index++;
        const c = text[pos];
        if (c === ',') { pos++; return; }
        if (t.obj ? c === '}' : c === ']') { pos++; stack.pop(); continue; }
        dead = true;
        return;
      }
    };

    skipWs();
    const first = text[pos];
    if (first === '{') stack.push({ obj: true, key: null, window: false, path: [] });
    else if (first === '[') stack.push({ obj: false, schedule: false, path: [], index: 0 });
    else return;
    pos++;

    for (;;) {
      skipWs();
      if (pos >= n || !top()) return;
      const t = top();
      const c = text[pos];
      if (c === (t.obj ? '}' : ']')) {
        pos++;
        stack.pop();
        finishValue();
        if (dead) break;
        continue;
      }
      if (t.obj) {
        if (c !== '"') return;
        const key = readString();
        if (key === null) return;
        t.key = key;
        skipWs();
        if (text[pos] !== ':') return;
        pos++;
        skipWs();
      } else if (c === ',') {
        pos++;
        continue;
      }
      const path = [...t.path, t.obj ? t.key : t.index];
      const v = text[pos];
      if (v === '{') {
        pos++;
        stack.push({ obj: true, key: null, window: !t.obj && t.schedule, path });
        continue;
      }
      if (v === '[') {
        pos++;
        stack.push({ obj: false, schedule: t.obj && t.key === 'schedule', path, index: 0 });
        continue;
      }
      if (v === '"') {
        if (readString() === null) return;
      } else if (v === '-' || (v >= '0' && v <= '9')) {
        const at = pos;
        const token = readNumber();
        // A malformed token (a bare '-', a leading zero) is not an offense:
        // JSON.parse refuses the file and names it.
        if (!/^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$/.test(token)) return;
        if (!t.obj && t.path[0] === 'providers'
            && ((t.path.length === 4 && t.index === 0)
                || (t.path.length === 5 && t.path[4] === 0))
            && !/^-?\d+$/.test(token)) {
          throw _pricingError(`${t.path[1]} via ${t.path[2]}[${t.path[3]}]: `
            + `timestamp delta at offset ${at} must be spelled a plain JSON integer`);
        }
        if (t.obj && t.window && (t.key === 'start' || t.key === 'end')
            && !/^-?\d+$/.test(token)) {
          offenses.push(`offset ${at} spells a schedule ${t.key} as ${token}`);
        }
      } else if (v === 't' && text.startsWith('true', pos)) {
        pos += 4;
      } else if (v === 'f' && text.startsWith('false', pos)) {
        pos += 5;
      } else if (v === 'n' && text.startsWith('null', pos)) {
        pos += 4;
      } else {
        return;
      }
      finishValue();
      if (dead) break;
    }
    if (offenses.length) {
      throw _pricingError(`${offenses.join('; ')}; a schedule start or end`
        + ' must be spelled a plain JSON integer');
    }
    if (offenses.length) {
      throw _pricingError(`${offenses.join('; ')}; a schedule start or end`
        + ' must be spelled a plain JSON integer');
    }
  };
  /* eslint-disable no-undef */
  if (typeof module !== 'undefined' && typeof module.exports !== 'undefined') {
    module.exports = { checkHhmmSpelling };   // node: the loaders' require chain
    return;
  }
  /* eslint-enable no-undef */
  window.checkHhmmSpelling = checkHhmmSpelling;   // the browser's script tag
})();
