"""Generate Phase 6 M8 evaluation figures.

Reads ``eval/results/all_summaries.json`` plus per-run CSVs and writes
PNG + PDF for each figure into ``eval/figures/``.

Figures:
  fig1_jct_bars.{png,pdf}        bar of jct_mean / p90 / p95 across E1..E5
  fig2_jct_cdf.{png,pdf}         CDF of per-job JCT for E1..E5
  fig3_slowdown_box.{png,pdf}    box plot of slowdown per experiment
  fig4_util_time.{png,pdf}       running utilization over time
  fig5_e6_heatmap.{png,pdf}      sensitivity heatmap of jct_mean over (alpha, delta)
  fig6_bf_rate.{png,pdf}         backfill rate / requeue counts
  fig7_jct_normalised.{png,pdf}  normalised improvement over E1 baseline
"""
from __future__ import annotations

import csv
import json
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RESULTS = os.path.join(ROOT, "eval", "results")
FIGS = os.path.join(ROOT, "eval", "figures")
os.makedirs(FIGS, exist_ok=True)

EXP_ORDER = ["e1", "e2", "e3", "e4", "e5", "e5b"]
EXP_LABEL = {
    "e1": "E1 FCFS",
    "e2": "E2 multifactor",
    "e3": "E3 score (M3)",
    "e4": "E4 score+pred (M5)",
    "e5": "E5 +frag (ckpt cost)",
    "e5b": "E5b +frag (no cost)",
}


def load_summary():
    """Load per-seed flat summaries."""
    with open(os.path.join(RESULTS, "all_summaries.json")) as fh:
        return json.load(fh)


def load_aggregated():
    path = os.path.join(RESULTS, "agg_by_run.json")
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return json.load(fh)


def primary_run(summaries, exp):
    """Pick a representative per-seed run for an experiment (for CSV plots).
    Multi-seed data may have many — we use the lowest seed for determinism."""
    rows = [s for s in summaries if s["experiment"] == exp]
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: r.get("synth_seed") or 0)
    return rows[0]


def agg_for(agg, exp):
    if not agg:
        return None
    rows = [s for s in agg if s["experiment"] == exp]
    return rows[0] if rows else None


def csv_for(exp, run):
    return os.path.join(RESULTS, exp, f"{run}.csv")


def save(fig, name):
    for ext in ("png", "pdf"):
        path = os.path.join(FIGS, f"{name}.{ext}")
        fig.savefig(path, dpi=160, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote eval/figures/{name}.{{png,pdf}}")


def fig1_jct_bars(summaries, agg):
    """Mean/p90/p95 JCT bars with 95% CI error bars across seeds."""
    means, p90s, p95s, labels = [], [], [], []
    err_mean, err_p90, err_p95 = [], [], []
    for exp in EXP_ORDER:
        a = agg_for(agg, exp) if agg else None
        s = primary_run(summaries, exp)
        if s is None and a is None:
            continue
        labels.append(EXP_LABEL[exp])
        if a is not None:
            means.append(a["jct_mean_mean"] / 3600.0)
            p90s.append(a["jct_p90_mean"] / 3600.0)
            p95s.append(a["jct_p95_mean"] / 3600.0)
            err_mean.append(a["jct_mean_ci95"] / 3600.0)
            err_p90.append(a["jct_p90_ci95"] / 3600.0)
            err_p95.append(a["jct_p95_ci95"] / 3600.0)
        else:
            means.append(s["jct_mean"] / 3600.0)
            p90s.append(s["jct_p90"] / 3600.0)
            p95s.append(s["jct_p95"] / 3600.0)
            err_mean.append(0); err_p90.append(0); err_p95.append(0)
    x = np.arange(len(labels))
    w = 0.27
    fig, ax = plt.subplots(figsize=(10.5, 4.8))
    ax.bar(x - w, means, w, yerr=err_mean, capsize=3, label="mean")
    ax.bar(x,     p90s,  w, yerr=err_p90,  capsize=3, label="p90")
    ax.bar(x + w, p95s,  w, yerr=err_p95,  capsize=3, label="p95")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=15, ha="right")
    ax.set_ylabel("Job Completion Time (hours)")
    n = (agg[0]["n_seeds"] if agg else 1)
    ax.set_title(f"JCT — mean / p90 / p95 across schedulers (error bars = 95% CI, n={n} seeds)")
    ax.legend()
    ax.grid(axis="y", linestyle=":", alpha=0.6)
    save(fig, "fig1_jct_bars")


def fig2_jct_cdf(summaries):
    fig, ax = plt.subplots(figsize=(8.5, 5))
    for exp in EXP_ORDER:
        s = primary_run(summaries, exp)
        if s is None:
            continue
        path = csv_for(exp, s["run"])
        jcts = []
        with open(path) as fh:
            r = csv.DictReader(fh)
            for row in r:
                if row["jct"]:
                    jcts.append(float(row["jct"]) / 3600.0)
        if not jcts:
            continue
        jcts.sort()
        y = np.arange(1, len(jcts) + 1) / len(jcts)
        ax.plot(jcts, y, label=EXP_LABEL[exp], linewidth=1.6)
    ax.set_xlabel("JCT (hours)")
    ax.set_ylabel("CDF (fraction of jobs ≤ x)")
    ax.set_title("Per-job JCT distribution (Philly subsample, 1000 jobs)")
    ax.set_xscale("log")
    ax.grid(linestyle=":", alpha=0.6)
    ax.legend()
    save(fig, "fig2_jct_cdf")


def fig3_slowdown_box(summaries):
    data, labels = [], []
    for exp in EXP_ORDER:
        s = primary_run(summaries, exp)
        if s is None:
            continue
        path = csv_for(exp, s["run"])
        slows = []
        with open(path) as fh:
            r = csv.DictReader(fh)
            for row in r:
                if row["slowdown"]:
                    slows.append(float(row["slowdown"]))
        if slows:
            data.append(slows)
            labels.append(EXP_LABEL[exp])
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.boxplot(data, labels=labels, showfliers=False)
    ax.set_ylabel("slowdown = JCT / max(runtime, 60s)")
    ax.set_title("Slowdown distribution (whiskers = 1.5·IQR; outliers hidden)")
    plt.setp(ax.get_xticklabels(), rotation=15, ha="right")
    ax.grid(axis="y", linestyle=":", alpha=0.6)
    save(fig, "fig3_slowdown_box")


def fig4_util_time(summaries):
    """Approximate utilisation timeline by binning per-job allocations."""
    fig, ax = plt.subplots(figsize=(9, 4.8))
    nodes = primary_run(summaries, "e1")["nodes"]
    gpn = primary_run(summaries, "e1")["gpus_per_node"]
    for exp in EXP_ORDER:
        s = primary_run(summaries, exp)
        if s is None:
            continue
        path = csv_for(exp, s["run"])
        intervals = []
        with open(path) as fh:
            r = csv.DictReader(fh)
            for row in r:
                if not row["start_ts"] or not row["end_ts"]:
                    continue
                st, en = float(row["start_ts"]), float(row["end_ts"])
                slots = float(row["mps_req"]) * float(row["gpu_count"])
                intervals.append((st, en, slots))
        if not intervals:
            continue
        t_max = max(en for _, en, _ in intervals)
        bins = 200
        edges = np.linspace(0, t_max, bins + 1)
        used = np.zeros(bins)
        cap_per_bin = nodes * gpn * 100  # mps slots
        for st, en, slots in intervals:
            i0 = int(st / t_max * bins)
            i1 = int(en / t_max * bins)
            i0, i1 = max(0, min(bins, i0)), max(0, min(bins, i1))
            if i1 > i0:
                used[i0:i1] += slots
        util = used / cap_per_bin
        centers = (edges[:-1] + edges[1:]) / 2 / 3600.0
        ax.plot(centers, util, label=EXP_LABEL[exp], linewidth=1.4)
    ax.set_xlabel("simulated time (hours)")
    ax.set_ylabel("utilization (used MPS slots / total)")
    ax.set_title("Cluster utilisation over time")
    ax.set_ylim(0, 1.05)
    ax.grid(linestyle=":", alpha=0.6)
    ax.legend()
    save(fig, "fig4_util_time")


def fig5_e6_heatmap(summaries, agg):
    rows = [s for s in (agg or []) if s["experiment"] == "e6"]
    field_mean = "jct_mean_mean"
    if not rows:
        rows = [s for s in summaries if s["experiment"] == "e6"]
        field_mean = "jct_mean"
    if not rows:
        return
    alphas = sorted({float(s["alpha"]) for s in rows})
    deltas = sorted({float(s["delta"]) for s in rows})
    z = np.zeros((len(deltas), len(alphas)))
    for s in rows:
        i = deltas.index(float(s["delta"]))
        j = alphas.index(float(s["alpha"]))
        z[i, j] = s[field_mean] / 3600.0
    fig, ax = plt.subplots(figsize=(6.5, 5))
    im = ax.imshow(z, origin="lower", cmap="viridis", aspect="auto")
    ax.set_xticks(range(len(alphas)))
    ax.set_xticklabels([f"{a:.2f}" for a in alphas])
    ax.set_yticks(range(len(deltas)))
    ax.set_yticklabels([f"{d:.2f}" for d in deltas])
    ax.set_xlabel("alpha (mps_fit weight)")
    ax.set_ylabel("delta (fragmentation penalty)")
    ax.set_title("E6 sensitivity — jct_mean (hours)")
    for i in range(z.shape[0]):
        for j in range(z.shape[1]):
            ax.text(j, i, f"{z[i, j]:.2f}", ha="center", va="center",
                    color="white" if z[i, j] < z.mean() else "black", fontsize=9)
    fig.colorbar(im, ax=ax, label="jct_mean (h)")
    save(fig, "fig5_e6_heatmap")


def fig6_bf_rate(summaries, agg):
    labels, bf, requeues = [], [], []
    for exp in EXP_ORDER:
        a = agg_for(agg, exp) if agg else None
        s = primary_run(summaries, exp)
        if s is None and a is None:
            continue
        labels.append(EXP_LABEL[exp])
        if a is not None:
            bf.append(a.get("bf_rate_mean", 0.0))
            requeues.append(a.get("requeue_count_mean", 0))
        else:
            bf.append(s.get("bf_rate", 0.0))
            requeues.append(s.get("requeue_count", 0))
    x = np.arange(len(labels))
    fig, ax1 = plt.subplots(figsize=(9, 4.5))
    ax1.bar(x - 0.18, bf, 0.36, color="C0", label="bf_rate")
    ax1.set_ylabel("backfill rate", color="C0")
    ax1.tick_params(axis="y", labelcolor="C0")
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels, rotation=15, ha="right")
    ax2 = ax1.twinx()
    ax2.bar(x + 0.18, requeues, 0.36, color="C3", label="requeues")
    ax2.set_ylabel("M7 requeues", color="C3")
    ax2.tick_params(axis="y", labelcolor="C3")
    ax1.set_title("Backfill rate vs M7 requeue count")
    ax1.grid(axis="y", linestyle=":", alpha=0.6)
    save(fig, "fig6_bf_rate")


def fig7_jct_normalised(summaries, agg):
    base_a = agg_for(agg, "e1") if agg else None
    base_s = primary_run(summaries, "e1")
    base_mean = base_a["jct_mean_mean"] if base_a else base_s["jct_mean"]
    labels, vals, errs = [], [], []
    for exp in EXP_ORDER:
        a = agg_for(agg, exp) if agg else None
        s = primary_run(summaries, exp)
        if s is None and a is None:
            continue
        labels.append(EXP_LABEL[exp])
        m = a["jct_mean_mean"] if a else s["jct_mean"]
        vals.append(1.0 - m / base_mean)
        errs.append((a["jct_mean_ci95"] / base_mean) if a else 0.0)
    x = np.arange(len(labels))
    fig, ax = plt.subplots(figsize=(10, 4.8))
    colors = ["#888", "C0", "C2", "C3", "C4", "C5"][: len(labels)]
    ax.bar(x, [v * 100 for v in vals], color=colors,
           yerr=[e * 100 for e in errs], capsize=3)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=15, ha="right")
    ax.set_ylabel("Mean-JCT improvement vs E1 (%)")
    n = (agg[0]["n_seeds"] if agg else 1)
    ax.set_title(f"Mean-JCT reduction relative to FCFS baseline (95% CI, n={n} seeds)")
    ax.grid(axis="y", linestyle=":", alpha=0.6)
    for xi, v in zip(x, vals):
        ax.text(xi, v * 100, f"{v*100:.1f}%", ha="center",
                va="bottom" if v >= 0 else "top", fontsize=9)
    save(fig, "fig7_jct_normalised")


def main() -> int:
    summaries = load_summary()
    agg = load_aggregated()
    fig1_jct_bars(summaries, agg)
    fig2_jct_cdf(summaries)
    fig3_slowdown_box(summaries)
    fig4_util_time(summaries)
    fig5_e6_heatmap(summaries, agg)
    fig6_bf_rate(summaries, agg)
    fig7_jct_normalised(summaries, agg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
