// Per-character advance of the dashboard's monospace face, measured once per
// font size and cached.
// It must be measured with getComputedTextLength() on a real SVG text node
// under a .dashboard ancestor, because app.css applies
//   .dashboard svg text { font-family: var(--mono); letter-spacing: 0.04em }
// and letter-spacing is invisible to canvas measureText. Ignoring it
// underestimates the width and consumes the labels' margin.
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
  return (monoAdvancePx._c[fontSize] = adv > 0 ? adv : fontSize * 0.64);
}

window.monoAdvancePx = monoAdvancePx;