(function () {
  window.Babel.disableScriptTags();

  window.addEventListener('DOMContentLoaded', () => {
    const entries = Array.from(document.querySelectorAll('script[type="text/babel"]'));
    let next = 0;
    let scheduled = false;
    let ready = false;
    let inlineCount = 0;
    // A message task, unlike a nested setTimeout, is never clamped to 4 ms.
    const channel = new MessageChannel();

    function schedule() {
      if (!ready || scheduled) return;
      scheduled = true;
      channel.port2.postMessage(null);
    }

    channel.port1.onmessage = () => {
      scheduled = false;
      while (next < entries.length && entries[next].error) next++;
      if (next === entries.length || !entries[next].loaded) return;
      const entry = entries[next++];
      const code = window.Babel.transform(entry.content, {
        presets: ['react'], plugins: [], filename: entry.url,
        sourceFileName: entry.url, sourceMaps: 'inline',
      }).code;
      const script = document.createElement('script');
      script.nonce = entry.nonce;
      // Strict as under Babel's default env preset; on line 1 so line numbers do not move.
      script.text = '"use strict";' + code;
      document.head.appendChild(script);
      schedule();
    };

    for (const entry of entries) {
      if (!entry.src) {
        inlineCount++;
        entry.url = 'Inline Babel script' + (inlineCount > 1 ? ` (${inlineCount})` : '');
        entry.content = entry.innerHTML;
        entry.loaded = true;
        continue;
      }
      entry.url = entry.src;
      // As Babel's own XHR runner: status 0 is a success, a failed read an empty script.
      fetch(entry.url, {mode: 'same-origin'}).then(response => {
        if (response.status !== 0 && response.status !== 200) {
          entry.error = true;
          schedule();
          setTimeout(() => { throw new Error('Could not load ' + entry.url); }, 0);
          return null;
        }
        return response.text().catch(() => '');
      }, () => '').then(content => {
        if (content === null) return;
        entry.content = content;
        entry.loaded = true;
        schedule();
      });
    }

    // The app prices at render, so no script runs before the rate tables exist.
    const readiness = [window.pricingReady
      || Promise.reject(new Error('pricing.json: the rate loader never ran'))];
    Promise.all(readiness).then(() => { ready = true; schedule(); }, (error) => {
      document.getElementById('root').textContent = error.message;
      throw error;
    });
  });
})();
