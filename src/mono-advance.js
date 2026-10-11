// Per-character advance of the dashboard's monospace face, measured once per
// font size and cached.
//
// It must be measured with getComputedTextLength() on a real SVG text node
// under a .dashboard ancestor, because app.css applies
//   .dashboard svg text { font-family: var(--mono); letter-spacing: 0.04em }
// and letter-spacing is invisible to canvas measureText. At 11px that is
// 0.44px/char — canvas reports 6.6px/char while the browser actually renders
// 7.04px/char. The old hard-coded 6.6 constant was exactly that canvas
// figure, and the missing 6.7% came out of the left margin: HBar's longest
// model label sat 3.6px from the panel edge.
//
// It lives in its own module because dashboard-charts.jsx (its only
// caller) sits exactly on its recorded size entry in
// .github/ci-thresholds.json, and that ratchet only ever moves down —
// #936 needed the lines this helper occupied, so the remedy the ratchet
// names (relocate the code into a new module) moved it here, called
// through window.monoAdvancePx.
function monoAdvancePx(fontSize) {
  monoAdvancePx._c = monoAdvancePx._c || {};
  if (monoAdvancePx._c[fontSize]) return monoAdvancePx._c[fontSize];
  const NS = 'http://www.w3.org/2000/svg';
  const host = document.createElement('div');
  host.className = 'dashboard';
  host.style.cssText =
    'position:absolute;left:-9999px;top:0;height:0;overflow:hidden;visibility:hidden';
  const svg = document.createElementNS(NS, 'svg');
  const text = document.createElementNS(NS, 'text');
  text.setAttribute('font-size', String(fontSize));
  text.setAttribute('font-family', 'monospace');
  const SAMPLE = '0123456789';
  text.textContent = SAMPLE;
  svg.appendChild(text);
  host.appendChild(svg);
  document.body.appendChild(host);
  // visibility:hidden still lays out, so this is a real measurement.
  const adv = text.getComputedTextLength() / SAMPLE.length;
  document.body.removeChild(host);
  // The face is monospace, so width === length * advance exactly; verified
  // against 8-, 9- and 10-character labels, all 7.041px/char at 11px.
  return (monoAdvancePx._c[fontSize] = adv > 0 ? adv : fontSize * 0.64);
}

window.monoAdvancePx = monoAdvancePx;