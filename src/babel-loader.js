(function () {
  window.Babel.disableScriptTags();

  window.addEventListener('DOMContentLoaded', () => {
    const entries = Array.from(document.querySelectorAll('script[type="text/babel"]'));
    let next = 0;
    let scheduled = false;
    let ready = false;
    let inlineCount = 0;

    function schedule() {
      if (!ready || scheduled) return;
      scheduled = true;
      setTimeout(() => {
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
        script.text = '"use strict";' + code;
        document.head.appendChild(script);
        schedule();
      }, 0);
    }

    for (const entry of entries) {
      if (!entry.src) {
        inlineCount++;
        entry.url = 'Inline Babel script' + (inlineCount > 1 ? ` (${inlineCount})` : '');
        entry.content = entry.innerHTML;
        entry.loaded = true;
        continue;
      }
      entry.url = entry.src;
      fetch(entry.url, {mode: 'same-origin'}).then(response => {
        if (response.status !== 0 && response.status !== 200) throw new Error();
        return response.text();
      }).then(content => {
        entry.content = content;
        entry.loaded = true;
        schedule();
      }, () => {
        entry.error = true;
        schedule();
        setTimeout(() => { throw new Error('Could not load ' + entry.url); }, 0);
      });
    }

    const readiness = [];
    Promise.all(readiness).then(() => { ready = true; schedule(); });
  });
})();
