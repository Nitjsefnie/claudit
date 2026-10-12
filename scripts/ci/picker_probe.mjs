// Serialized into the page: collect every state without per-page protocol hops.
export function pickerProbe(input) {
  const read = (sel) => {
    const strip = document.querySelector(sel);
    if (!strip) return null;
    const box = strip.getBoundingClientRect();
    const pager = strip.querySelector('.pp-pager');
    const pagerBox = pager ? pager.getBoundingClientRect() : null;
    const jump = pager && pager.querySelector('.pp-jump');
    const count = strip.querySelector('.pp-count')?.textContent || '';
    const parts = count.match(/^\s*(\d+)\s*\/\s*(\d+)\s*$/);
    const visible = [...strip.children].filter((el) => {
      if (!el.classList.contains('pp-proj')) return false;
      const cs = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return cs.display !== 'none' && cs.visibility !== 'hidden'
        && r.width > 0 && r.height > 0;
    });
    const chips = [...strip.children].filter((el) =>
      el.classList.contains('pp-proj'));
    const rects = visible.map((el) => el.getBoundingClientRect());
    const next = strip.querySelector('.pp-nav[title="Next page"]');
    const previous = strip.querySelector('.pp-nav[title="Previous page"]');
    const below = strip.nextElementSibling
      ? strip.nextElementSibling.getBoundingClientRect() : null;
    return {
      left: box.left, right: box.right,
      top: Math.round(box.top * 10) / 10,
      height: Math.round(box.height * 10) / 10,
      seat: below ? Math.round((below.top - box.bottom) * 10) / 10 : 0,
      below: !!below,
      overflow: strip.scrollWidth > strip.clientWidth,
      chipRight: rects.reduce((n, r) => Math.max(n, r.right), 0),
      ids: visible.map((el) => el.dataset.projectId || null),
      activeIds: visible.filter((el) => el.classList.contains('on'))
        .map((el) => el.dataset.projectId || null),
      pagerLeft: pagerBox ? pagerBox.left : null,
      pagerRight: pagerBox ? pagerBox.right : null,
      pagerWidth: pagerBox ? pagerBox.width : null,
      pagerVisible: !!pager && getComputedStyle(pager).display !== 'none'
        && getComputedStyle(pager).visibility !== 'hidden'
        && pagerBox.width > 0,
      jumpWidth: jump ? jump.getBoundingClientRect().width : null,
      jumpVisible: !!jump && getComputedStyle(jump).visibility !== 'hidden'
        && getComputedStyle(jump).display !== 'none',
      page: parts ? Number(parts[1]) : (pager ? null : 1),
      total: parts ? Number(parts[2]) : (pager ? null : 1),
      nextDisabled: !next || next.disabled,
      previousDisabled: !previous || previous.disabled,
      rendered: chips.length,
    };
  };
  if (typeof input === 'string') return read(input);
  const rendered = () => new Promise((resolve) => requestAnimationFrame(
    () => requestAnimationFrame(resolve)));
  return (async () => {
    const {selector, maxPages, direction, target} = input;
    const states = [];
    if (input.widths) {
      const strip = document.querySelector(selector);
      for (const width of input.widths) {
        strip.style.width = width + 'px';
        await rendered();
        states.push({width, observed: read(selector)});
      }
      return states;
    }
    const forward = direction === 'forward';
    for (let step = 0; step < maxPages; step++) {
      const state = read(selector);
      if (!state) return {states, completed: false, missing: true};
      states.push(state);
      if ((target && state.ids.includes(target))
        || (forward ? state.nextDisabled : state.previousDisabled)) {
        return {states, completed: true, missing: false};
      }
      if (step === maxPages - 1) break;
      const title = forward ? 'Next page' : 'Previous page';
      const strip = document.querySelector(selector);
      await new Promise((resolve, reject) => {
        const changed = () => Number(strip.querySelector('.pp-count')
          ?.textContent.split('/')[0]) !== state.page;
        const finish = () => {
          if (!changed()) return;
          observer.disconnect(); clearTimeout(timer); resolve();
        };
        const observer = new MutationObserver(finish);
        const timer = setTimeout(() => {
          observer.disconnect(); reject(new Error('picker page did not advance'));
        }, 5000);
        observer.observe(strip, {subtree: true, childList: true, characterData: true});
        strip.querySelector(`[title="${title}"]`).click();
        finish();
      });
      // Give the committed page a render turn, including queued DOM seeds.
      await new Promise((resolve) => requestAnimationFrame(resolve));
    }
    return {states, completed: false, missing: false};
  })();
}
