// The vendor tables (SV-RATE-DATA): the bare first-party form each
// vendor_host-carrying tracked entry prices, and its host. A tracked
// entry without vendor_host carries no first-party pricing and takes no
// part. A bare form two entries share, or one that is also a
// models-table key, is an ambiguity that would silently pick one price:
// refuse naming both rows. Mirrors pricing_load._vendor_tables.
//
// Loaded ahead of src/pricing-loader.js by index.html's script tags (it
// installs the builder on window) and required by the loader under node;
// the loader calls it once its own tables are built. Any failure throws
// naming pricing.json: with no valid tables there is no honest price.
(function () {
  const _pricingError = (detail) => new Error(`pricing.json: ${detail}`);
  const _VENDOR_NAME = /^[a-z0-9-]+$/;

  const buildVendorTables = (pricing, modelRates, vendorBare, vendorHosts) => {
    const openrouter = pricing.openrouter;
    if (openrouter === null || typeof openrouter !== 'object') {
      throw _pricingError('openrouter is missing');
    }
    const vendor = openrouter.vendor;
    const prefixes = vendor && typeof vendor === 'object' ? vendor.prefixes : null;
    if (!Array.isArray(prefixes) || !prefixes.length
        || !prefixes.every((p) => typeof p === 'string' && p && _VENDOR_NAME.test(p))
        || new Set(prefixes).size !== prefixes.length) {
      throw _pricingError('openrouter.vendor.prefixes is missing or not a '
        + 'list of distinct lowercase [a-z0-9-] namespace strings');
    }
    const tracked = openrouter.models;
    if (tracked === null || typeof tracked !== 'object') {
      throw _pricingError('openrouter.models is missing');
    }
    for (const [key, entry] of Object.entries(tracked)) {
      const where = `openrouter.models[${JSON.stringify(key)}]`;
      if (entry === null || typeof entry !== 'object' || Array.isArray(entry)) {
        throw _pricingError(`${where} is not a mapping`);
      }
      if (typeof entry.id !== 'string' || !entry.id) {
        throw _pricingError(`${where} carries no 'id'`);
      }
      const host = entry.vendor_host;
      if (host === undefined || host === null) continue;
      if (typeof host !== 'string' || !host) {
        throw _pricingError(`${where}: vendor_host is not a non-empty string`);
      }
      let form = key;
      for (const prefix of [...prefixes].sort((a, b) => b.length - a.length)) {
        if (key.startsWith(`${prefix}/`)) {
          form = key.slice(prefix.length + 1);
          break;
        }
      }
      if (form in vendorBare) {
        throw _pricingError(`${where} and openrouter.models[`
          + `${JSON.stringify(vendorBare[form])}] both carry the bare `
          + `form ${JSON.stringify(form)}`);
      }
      if (form in modelRates) {
        throw _pricingError(`${where}: bare form ${JSON.stringify(form)} is `
          + 'also a models-table key; a transcript id would resolve two ways');
      }
      vendorBare[form] = key;
      vendorHosts[key] = host;
    }
  };

  /* eslint-disable no-undef */
  if (typeof module !== 'undefined' && typeof module.exports !== 'undefined') {
    module.exports = { buildVendorTables };   // node: the loaders' require chain
    return;
  }
  /* eslint-enable no-undef */
  window.buildVendorTables = buildVendorTables;   // the browser's script tag
})();
