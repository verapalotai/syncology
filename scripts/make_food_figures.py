"""Render the write-up figures for the food-retrieval study.

Reproducible-as-figures: the metrics below mirror the tables in
``docs/food_reconciliation_report.md`` (produced by the eval scripts — benchmark,
rigor, cascade, ditto, hyde). Static PNGs for the Quarto post live in ``docs/figures``.

Palette: Okabe-Ito — colourblind-safe by construction, the scientific-publishing
standard; categorical hues assigned in fixed order, never cycled.

Usage:  uv run python scripts/make_food_figures.py   (needs the `viz` extra)
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt

OUT = Path("docs/figures")
# Okabe-Ito
INK, MUTED = "#1a1a1a", "#8a8a8a"
ORANGE, SKY, GREEN, BLUE, VERM, PURPLE = (
    "#E69F00", "#56B4E9", "#009E73", "#0072B2", "#D55E00", "#CC79A7")

plt.rcParams.update({
    "font.size": 11, "font.family": "sans-serif", "axes.edgecolor": MUTED,
    "axes.linewidth": 0.8, "text.color": INK, "axes.labelcolor": INK,
    "xtick.color": MUTED, "ytick.color": MUTED, "figure.dpi": 150,
    "svg.fonttype": "none",
})


def _clean(ax, grid_axis="x"):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(axis=grid_axis, color="#e6e6e6", linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    ax.tick_params(length=0)


def fig_translation():
    """Dumbbell: raw → translated Success@1 per embedder (the one lever)."""
    rows = [("bge-m3 (0.6B)", 0.299, 0.877), ("qwen3-0.6b", 0.255, 0.853),
            ("harrier-0.6b", 0.279, 0.853), ("qwen3-4b (4B)", 0.387, 0.828)]
    fig, ax = plt.subplots(figsize=(7.2, 3.2))
    ys = range(len(rows))
    for y, (_, raw, tr) in zip(ys, rows):
        ax.plot([raw, tr], [y, y], color="#cfcfcf", lw=3, zorder=1, solid_capstyle="round")
        ax.scatter(raw, y, s=90, color=ORANGE, zorder=3)
        ax.scatter(tr, y, s=90, color=BLUE, zorder=3)
        ax.annotate(f"{raw:.2f}", (raw, y), xytext=(-8, 0), textcoords="offset points",
                    va="center", ha="right", fontsize=9, color=MUTED)
        ax.annotate(f"{tr:.2f}", (tr, y), xytext=(8, 0), textcoords="offset points",
                    va="center", ha="left", fontsize=9, color=INK, weight="bold")
    ax.set_yticks(list(ys))
    ax.set_yticklabels([r[0] for r in rows])
    ax.set_ylim(-0.6, len(rows) - 0.4)
    ax.set_xlim(0.1, 1.0)
    ax.set_xlabel("Success@1 (hard set, N=204)")
    ax.scatter([], [], color=ORANGE, s=90, label="raw name")
    ax.scatter([], [], color=BLUE, s=90, label="translated")
    ax.legend(loc="upper left", frameon=False, fontsize=10, handletextpad=0.4)
    ax.set_title("One transform closes the gap — the embedder barely matters",
                 fontsize=12, weight="bold", loc="left", color=INK)
    _clean(ax)
    fig.tight_layout()
    fig.savefig(OUT / "fig_translation.png", bbox_inches="tight")
    plt.close(fig)


def fig_cascade():
    """Cascade accuracy-vs-threshold frontier with baselines."""
    taus = [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85]
    acc = [0.877, 0.877, 0.877, 0.877, 0.892, 0.897, 0.897, 0.892, 0.868, 0.863]
    fig, ax = plt.subplots(figsize=(7.2, 3.8))
    ax.axvspan(0.60, 0.75, color="#eaf3f8", zorder=0)
    ax.axhline(0.877, color=MUTED, ls="--", lw=1.2, zorder=1)
    ax.axhline(0.863, color=VERM, ls="--", lw=1.2, zorder=1)
    ax.plot(taus, acc, color=BLUE, lw=2.4, zorder=3, solid_capstyle="round")
    ax.scatter(taus, acc, s=28, color=BLUE, zorder=4)
    ax.scatter([0.70], [0.897], s=150, facecolor="white", edgecolor=VERM,
               linewidth=2.2, zorder=5)
    ax.annotate("operating point τ*=P10\n(route least-confident 10%)  0.897",
                (0.70, 0.897), xytext=(0.505, 0.915), fontsize=9, color=INK,
                arrowprops=dict(arrowstyle="-", color=MUTED, lw=0.8))
    ax.annotate("USDA-only  0.877", (0.40, 0.877), xytext=(0.402, 0.881),
                fontsize=9, color=MUTED)
    ax.annotate("naïve merge (max-confidence)  0.863", (0.40, 0.863),
                xytext=(0.402, 0.849), fontsize=9, color=VERM)
    ax.set_xlabel("USDA confidence threshold τ  (route to OFF when cosine < τ)")
    ax.set_ylabel("Success@1")
    ax.set_title("Coverage, gated: a broad τ-window beats both baselines",
                 fontsize=12, weight="bold", loc="left", color=INK)
    ax.set_ylim(0.845, 0.92)
    ax.set_xlim(0.39, 0.86)
    _clean(ax, grid_axis="y")
    fig.tight_layout()
    fig.savefig(OUT / "fig_cascade.png", bbox_inches="tight")
    plt.close(fig)


def fig_ditto():
    """Ditto attribute-serialization per stratum (name-only vs +macros)."""
    strata = ["simple", "compound", "prepared", "branded", "regional"]
    name = [0.877, 0.905, 0.927, 0.704, 0.000]
    ditto = [0.860, 0.905, 0.927, 0.815, 0.000]
    fig, ax = plt.subplots(figsize=(7.2, 3.6))
    x = range(len(strata))
    w = 0.38
    ax.bar([i - w / 2 for i in x], name, w, color=SKY, label="name-only", zorder=2)
    ax.bar([i + w / 2 for i in x], ditto, w, color=GREEN, label="+ macros (Ditto)", zorder=2)
    ax.annotate("+11 pp", (3, 0.815), xytext=(3, 0.9), ha="center", fontsize=10,
                weight="bold", color=GREEN,
                arrowprops=dict(arrowstyle="->", color=GREEN, lw=1.2))
    for i, (n, d) in enumerate(zip(name, ditto)):
        ax.text(i - w / 2, n + 0.015, f"{n:.2f}", ha="center", fontsize=8, color=MUTED)
        ax.text(i + w / 2, d + 0.015, f"{d:.2f}", ha="center", fontsize=8, color=INK)
    ax.set_xticks(list(x))
    ax.set_xticklabels([f"{s}\nN={n}" for s, n in
                        zip(strata, [114, 21, 55, 27, 3])], fontsize=9)
    ax.set_ylabel("Success@1")
    ax.set_ylim(0, 1.02)
    ax.legend(loc="upper right", frameon=False, fontsize=10)
    ax.set_title("Serializing macros is a targeted win — only on branded",
                 fontsize=12, weight="bold", loc="left", color=INK)
    _clean(ax, grid_axis="y")
    fig.tight_layout()
    fig.savefig(OUT / "fig_ditto.png", bbox_inches="tight")
    plt.close(fig)


def fig_sophistication():
    """Diverging Δ Success@1 vs the translated baseline — nothing beats translation."""
    # (label, delta, significant?)
    data = [("hybrid (RRF)", +0.005, False), ("ColBERT", 0.000, False),
            ("sparse (BM25)", -0.029, False), ("cross-encoder rerank", -0.044, True),
            ("4B model (qwen3-4b)", -0.049, False), ("HyDE elaboration", -0.112, True)]
    data.sort(key=lambda d: d[1])
    fig, ax = plt.subplots(figsize=(7.2, 3.6))
    ys = range(len(data))
    for y, (_, d, sig) in zip(ys, data):
        ax.barh(y, d, color=(VERM if d < 0 else BLUE), height=0.62, zorder=2)
        off = -0.004 if d < 0 else 0.004
        ax.text(d + off, y, f"{d:+.3f}" + (" *" if sig else ""),
                va="center", ha="right" if d < 0 else "left", fontsize=9,
                color=INK, weight="bold" if sig else "normal")
    ax.axvline(0, color=INK, lw=1.0, zorder=3)
    ax.set_yticks(list(ys))
    ax.set_yticklabels([d[0] for d in data])
    ax.set_xlim(-0.16, 0.06)
    ax.set_xlabel("Δ Success@1 vs translated baseline (0.877)")
    ax.set_title("Every step past translation is flat or harmful  (* p<0.05)",
                 fontsize=12, weight="bold", loc="left", color=INK)
    ax.text(0.02, 0.06, "for scale: translation itself is +0.58",
            transform=ax.transAxes, fontsize=9, color=MUTED, style="italic")
    _clean(ax, grid_axis="x")
    fig.tight_layout()
    fig.savefig(OUT / "fig_sophistication.png", bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    fig_translation()
    fig_cascade()
    fig_ditto()
    fig_sophistication()
    print(f"wrote 4 figures to {OUT}/")


if __name__ == "__main__":
    main()
