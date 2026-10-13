const LEGEND_SWATCH = 20, LEGEND_GAP = 6, LEGEND_SPACING = 24;
function burnRateLegend(series, rateVisible, legendAdvUsed, availableWidth) {
  const legendItems = Object.entries(series).map(([k, s]) => (
    { key: k, color: s.color, label: `${s.label} (EMA)` }));
  if (rateVisible) legendItems.push({ key: '__ratelimit', color: '#ff3366', label: 'Rate limit hit' });
  // Wrap instead of running off the panel: at a 800px viewport the
  // five entries need ~680px against ~630px of usable width, and an
  // unwrapped row put text 23px outside the svg.
  const legendAvail = Math.max(120, availableWidth);
  let legendCx = 0, legendRow = 0;
  const legendPlaced = legendItems.map(it => {
    const wEntry = LEGEND_SWATCH + LEGEND_GAP + it.label.length * legendAdvUsed
      + LEGEND_SPACING;
    if (legendCx > 0 && legendCx + wEntry > legendAvail) { legendRow += 1; legendCx = 0; }
    const at = legendCx, row = legendRow;
    legendCx += wEntry;
    return { ...it, at, row };
  });
  const legendRows = legendRow + 1;
  return { legendPlaced, legendRows };
}
window.burnRateLegend = burnRateLegend;
