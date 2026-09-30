// Shared Inspector timeline-selection semantics (issue #364). A search or
// filter can shrink the filtered timeline under the stored selection index;
// every consumer — the detail row, the keyboard moves, the selected row and
// the listbox's aria-activedescendant — must derive from the ONE clamped
// index, or the active option, the selected row and the detail pane
// disagree.

function activeTimelineIndex(selected, count) {
  return Math.min(selected, count - 1);
}

function moveTimelineIndex(active, count, delta) {
  if (count <= 0) return -1;
  return Math.max(0, Math.min(count - 1, active + delta));
}

Object.assign(window, { activeTimelineIndex, moveTimelineIndex });
