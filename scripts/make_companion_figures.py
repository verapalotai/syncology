"""Figures for the companion write-ups (biomarker resolution, cycle inference).

Reproducible-as-figures: numbers mirror `scripts/eval_resolvers.py` and the cycle
report's validation section (aggregate metrics only — no individual measurements).
Palette: Okabe-Ito, matching the food-retrieval figures.

Usage:  uv run python scripts/make_companion_figures.py   (needs the `viz` extra)
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt

OUT = Path("docs/figures")
INK, MUTED = "#1a1a1a", "#8a8a8a"
ORANGE, SKY, GREEN, BLUE, VERM, PURPLE = (
    "#E69F00", "#56B4E9", "#009E73", "#0072B2", "#D55E00", "#CC79A7")

plt.rcParams.update({
    "font.size": 11, "font.family": "sans-serif", "axes.edgecolor": MUTED,
    "axes.linewidth": 0.8, "text.color": INK, "axes.labelcolor": INK,
    "xtick.color": MUTED, "ytick.color": MUTED, "figure.dpi": 150,
})


def _clean(ax, grid_axis="y"):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(axis=grid_axis, color="#e6e6e6", linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    ax.tick_params(length=0)


def fig_biomarker_regime():
    """The regime flip: rules win the closed vocab, the LLM wins the open tail."""
    groups = ["Curated vocabulary\n(148 labelled names)", "Novel names\n(absent from alias dict)"]
    rule = [1.00, 0.00]
    llm = [0.986, 1.00]
    fig, ax = plt.subplots(figsize=(7.2, 3.7))
    x = range(len(groups))
    w = 0.36
    ax.bar([i - w / 2 for i in x], rule, w, color=BLUE, label="rule-based (free)", zorder=2)
    ax.bar([i + w / 2 for i in x], llm, w, color=ORANGE, label="LLM — Haiku ($0.02/run)", zorder=2)
    for i, (r, m) in enumerate(zip(rule, llm)):
        ax.text(i - w / 2, r + 0.02, f"{r:.0%}", ha="center", fontsize=9, color=INK, weight="bold")
        ax.text(i + w / 2, m + 0.02, f"{m:.1%}" if m not in (0, 1) else f"{m:.0%}",
                ha="center", fontsize=9, color=INK, weight="bold")
    ax.annotate("dictionary\nabstains", (1 - w / 2, 0.02), xytext=(1 - w / 2, 0.30),
                ha="center", fontsize=8.5, color=VERM,
                arrowprops=dict(arrowstyle="->", color=VERM, lw=1.1))
    ax.set_xticks(list(x))
    ax.set_xticklabels(groups, fontsize=9.5)
    ax.set_ylabel("Accuracy")
    ax.set_ylim(0, 1.12)
    ax.set_yticks([0, 0.25, 0.5, 0.75, 1.0])
    ax.legend(loc="upper center", frameon=False, fontsize=10, ncol=2, bbox_to_anchor=(0.5, 1.14))
    ax.set_title("Match the method to the regime", fontsize=12, weight="bold",
                 loc="left", color=INK, pad=24)
    _clean(ax)
    fig.tight_layout()
    fig.savefig(OUT / "fig_biomarker_regime.png", bbox_inches="tight")
    plt.close(fig)


def fig_cycle_biphasic():
    """The biphasic BBT curve emerging from inferred phases — the validity check."""
    phases = ["follicular", "ovulation", "luteal"]
    bbt = [36.50, 36.54, 36.83]
    colors = [SKY, SKY, VERM]
    fig, ax = plt.subplots(figsize=(7.2, 3.4))
    ax.axhspan(36.44, 36.60, color="#eaf3f8", zorder=0)  # low plateau band
    for i, (p, t, c) in enumerate(zip(phases, bbt, colors)):
        ax.plot([i, i], [36.40, t], color="#d9d9d9", lw=2, zorder=1)
        ax.scatter(i, t, s=170, color=c, zorder=3)
        ax.text(i, t + 0.012, f"{t:.2f} °C", ha="center", fontsize=10,
                color=INK, weight="bold")
    ax.annotate("", xy=(2, 36.83), xytext=(2, 36.52),
                arrowprops=dict(arrowstyle="<->", color=MUTED, lw=1.2))
    ax.text(2.06, 36.675, "+0.33 °C\nconfirmed shift", va="center", fontsize=9, color=INK)
    ax.text(0.5, 36.455, "low follicular plateau", fontsize=8.5, color="#4a7ba6", style="italic")
    ax.set_xticks(range(len(phases)))
    ax.set_xticklabels([f"{p}\n(inferred)" for p in phases])
    ax.set_ylabel("Mean basal body temperature")
    ax.set_ylim(36.40, 36.90)
    ax.set_xlim(-0.5, 2.7)
    ax.set_title("The inferred phases reproduce the textbook biphasic curve",
                 fontsize=12, weight="bold", loc="left", color=INK)
    _clean(ax)
    fig.tight_layout()
    fig.savefig(OUT / "fig_cycle_biphasic.png", bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    fig_biomarker_regime()
    fig_cycle_biphasic()
    print(f"wrote 2 companion figures to {OUT}/")


if __name__ == "__main__":
    main()
