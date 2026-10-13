"""Burn-rate marker artists, legend handles and their shared styles."""
from __future__ import annotations

try:
    from matplotlib.lines import Line2D
except ImportError:
    Line2D = None

# -- Burn rate constants --
COLOR_LIMIT_HIT = "#ff3366"
COLOR_WINDOW = "#ffffff"

BURN_TOKEN_STYLES = {
    "output":       {"color": "#ee4444", "lw": 1.5, "alpha": 0.85, "label": "Output"},
    "input":        {"color": "#44dd66", "lw": 1.5, "alpha": 0.85, "label": "Input"},
    "cache_create": {"color": "#dd66aa", "lw": 1.5, "alpha": 0.85, "label": "Cache Create"},
    "cache_read":   {"color": "#44bbbb", "lw": 1.5, "alpha": 0.85, "label": "Cache Read"},
}

MODEL_COLORS = {
    "opus-4-7": "#ff2222",
    "opus-4-6": "#ff8800",
    "opus-4-5": "#ffdd00",
    "sonnet-4-6": "#00bbff",
    "sonnet-4-5": "#8866ff",
    "haiku-4-5": "#88cc44",
}


def _burn_draw_markers(ax, ctx, window_boundaries, limit_hits, show_rate_limits: bool = True):
    """Window boundary lines + rate-limit hit markers."""
    for wb in window_boundaries:
        if wb < ctx["xlim_start"] or wb > ctx["xlim_end"]:
            continue
        ax.axvline(wb, color=COLOR_WINDOW, alpha=0.12, linewidth=1, linestyle=":", zorder=1)

    visible_hits = [h for h in limit_hits if show_rate_limits and ctx["xlim_start"] <= h["ts"] <= ctx["xlim_end"]]
    for hit in visible_hits:
        ax.axvline(hit["ts"], color=COLOR_LIMIT_HIT, alpha=0.7, linewidth=2, zorder=9)
    ctx["visible_hits"] = visible_hits


def _burn_legend_handles(ctx, show_rate_limits: bool = True):
    """One legend handle per token EMA, window marker, model and dot size."""
    if Line2D is None:
        raise RuntimeError("matplotlib is required to render plots")
    handles = []
    for key in ctx["token_keys"]:
        style = BURN_TOKEN_STYLES[key]
        handles.append(Line2D([0], [0], color=style["color"],
                              linewidth=style["lw"],
                              alpha=style["alpha"],
                              label=f"{style['label']} (EMA)"))
    handles.append(Line2D([0], [0], color=COLOR_WINDOW, alpha=0.3,
                          linewidth=1, linestyle=":",
                          label="Window start (5h+ gap)"))
    if show_rate_limits and ctx["visible_hits"]:
        handles.append(Line2D([0], [0], color=COLOR_LIMIT_HIT,
                              linewidth=2, label="Rate limit hit"))
    for model in sorted(set(s["primary_model"] for s in ctx["visible"])):
        c = MODEL_COLORS.get(model, "#888888")
        handles.append(Line2D([0], [0], marker="o", color="none",
                              markerfacecolor=c, markeredgecolor="white",
                              markeredgewidth=0.3, markersize=8,
                              alpha=0.6, label=model))
    for dur_label, dur_h in [("30m", 0.5), ("1h", 1), ("4h", 4)]:
        sz = min(max(dur_h * 60, 25), 250)
        handles.append(Line2D([0], [0], marker="o", color="none",
                              markerfacecolor="#888888",
                              markeredgecolor="white",
                              markeredgewidth=0.3,
                              markersize=sz ** 0.5,
                              alpha=0.4, label=dur_label))
    return handles
