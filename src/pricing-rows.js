// Provider storage and grouped-history helpers, mirrored by backend/pricing_rows.py.
(function () {
  const _pricingError = (detail) => new Error(`pricing.json: ${detail}`);
  // Positional provider rows mirror backend/pricing_rows.py. Decoding retains
  // grouped instants; only the table builder flattens them into dated windows.
  function decodePricingDoc(doc) {
    if (doc._format !== 'pricing-rows/3') {
      throw _pricingError("_format must be 'pricing-rows/3'");
    }
    const out = { ...doc, providers: {} };
    delete out._format;
    const object = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
    const delta = (v, at) => {
      if (!Number.isSafeInteger(v) || v < 0 || v > 253402300799) {
        throw _pricingError(`${at}: timestamp delta is not a non-negative integer in range`);
      }
      return v;
    };
    if (!object(doc.providers)) throw _pricingError('providers is not an object');
    for (const [model, hosts] of Object.entries(doc.providers)) {
      if (!object(hosts)) throw _pricingError(`${model}: providers are not an object`);
      out.providers[model] = {};
      for (const [host, rows] of Object.entries(hosts)) {
        const where = `${model} via ${host}`;
        if (!Array.isArray(rows) || !rows.length) {
          throw _pricingError(`${where}: expected a non-empty provider series`);
        }
        let previous = null;
        const seen = new Set();
        out.providers[model][host] = rows.map((row, i) => {
          const at = `${where}[${i}]`;
          if (!Array.isArray(row) || row.length < 4 || row.length > 6) {
            throw _pricingError(`${at}: expected a positional provider row of 4 to 6 slots`);
          }
          const entry = { fresh: row[1], output: row[2], read: row[3] };
          const tail = row.slice(4);
          if (tail.length && !object(tail[0])) {
            const note = tail.shift();
            if (typeof note === 'string') entry.note = note;
            else if (typeof note === 'number' && Number.isFinite(note)) entry.note = `${note}% off`;
            else throw _pricingError(`${at}: note is not a string or finite number`);
          }
          if (tail.length) {
            if (tail.length !== 1 || !object(tail[0])
                || Object.keys(tail[0]).some((k) => ['from', 'note', 'fresh', 'output', 'read'].includes(k))) {
              throw _pricingError(`${at}: invalid provider row rest object`);
            }
            Object.assign(entry, tail[0]);
          }
          if (row[0] === null) {
            if (i) throw _pricingError(`${at}: only the first entry has no 'from'`);
            entry.from = null;
          } else {
            const deltas = Array.isArray(row[0]) ? row[0] : [row[0]];
            if (!deltas.length) throw _pricingError(`${at}: timestamp list is empty`);
            const seconds = [];
            const stamps = deltas.map((d, j) => {
              delta(d, at);
              if ((j || previous !== null) && d === 0) {
                throw _pricingError(`${at}: timestamp instants are not distinct`);
              }
              const second = d + (j ? seconds[j - 1] : previous || 0);
              delta(second, at);
              if (seen.has(second)) throw _pricingError(`${at}: timestamp instants are not distinct`);
              seen.add(second);
              seconds.push(second);
              return new Date(second * 1000).toISOString().replace('.000Z', 'Z');
            });
            entry.from = Array.isArray(row[0]) ? stamps : stamps[0];
            previous = seconds[0];
          }
          return entry;
        });
      }
    }
    return out;
  }

  function flattenPricingHistory(entries, where, instantMs) {
    if (!entries.some((e) => Array.isArray(e.from))) {
      return { entries, owners: entries.map((_, i) => i) };
    }
    const points = [], seen = new Set();
    let previousFirst = null;
    entries.forEach((entry, i) => {
      const at = `${where}[${i}]`;
      const stamps = Array.isArray(entry.from) ? entry.from : [entry.from ?? null];
      if (!stamps.length) throw _pricingError(`${at}: timestamp list is empty`);
      let previous = null;
      stamps.forEach((stamp, j) => {
        if (stamp === null && (i || j || stamps.length > 1)) {
          throw _pricingError(`${at}: only the first entry has no 'from'`);
        }
        const instant = stamp === null ? -Infinity : instantMs(stamp);
        if (Number.isNaN(instant)) throw _pricingError(`${at}: invalid timestamp`);
        if (seen.has(instant) || (previous !== null && instant <= previous)
            || (j === 0 && previousFirst !== null && instant <= previousFirst)) {
          throw _pricingError(`${at}: timestamp instants are not distinct and increasing`);
        }
        if (j === 0) previousFirst = instant;
        seen.add(instant);
        points.push({ instant, owner: i, entry: { ...entry, from: stamp } });
        previous = instant;
      });
    });
    points.sort((a, b) => a.instant - b.instant);
    return { entries: points.map((p) => p.entry), owners: points.map((p) => p.owner) };
  }

  const exported = { decodePricingDoc, flattenPricingHistory };
  /* eslint-disable no-undef */
  if (typeof module !== 'undefined' && typeof module.exports !== 'undefined') {
    module.exports = exported;
  } else Object.assign(window, exported);
  /* eslint-enable no-undef */
})();
