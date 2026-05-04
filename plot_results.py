"""
plot_results.py — generate benchmark comparison graphs from Locust CSV output.

Usage:
    python3 plot_results.py                    # reads results/ automatically
    python3 plot_results.py --dir results/     # explicit directory
    python3 plot_results.py --out graphs/      # custom output directory

Generates:
    1. rps_vs_users.png         — throughput per algorithm
    2. avg_latency_vs_users.png — mean response time
    3. p95_latency_vs_users.png — tail latency (p95)
    4. p99_latency_vs_users.png — tail latency (p99)
    5. failure_rate.png         — failure % heatmap
    6. latency_distribution.png — p50/p95/p99 grouped bar chart (all algos, 400 users)
    7. summary_radar.png        — radar chart normalised across all metrics
"""

import os
import sys
import glob
import argparse
import math
import csv
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")   # headless — no display needed
import matplotlib.pyplot as plt
import matplotlib.cm as cm
import numpy as np

# ─── Configuration ────────────────────────────────────────────────────────────

ALGORITHMS = ["round_robin", "random", "ip_hash", "least_connections", "weighted"]
USER_COUNTS = [100, 200, 400, 600, 800]

ALGO_LABELS = {
    "round_robin":       "Round Robin",
    "random":            "Random",
    "ip_hash":           "IP Hash",
    "least_connections": "Least Connections",
    "weighted":          "Weighted RR",
}

COLORS = {
    "round_robin":       "#2196F3",
    "random":            "#FF9800",
    "ip_hash":           "#4CAF50",
    "least_connections": "#F44336",
    "weighted":          "#9C27B0",
}

MARKERS = {
    "round_robin":       "o",
    "random":            "s",
    "ip_hash":           "^",
    "least_connections": "D",
    "weighted":          "P",
}

# ─── CSV parsing ──────────────────────────────────────────────────────────────

def parse_stats_csv(path):
    """Return the Aggregated row from a Locust *_stats.csv as a dict."""
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("Name") == "Aggregated":
                return {
                    "requests":  int(row.get("Request Count", 0)),
                    "failures":  int(row.get("Failure Count", 0)),
                    "avg_ms":    float(row.get("Average Response Time", 0)),
                    "p50_ms":    float(row.get("50%", 0)),
                    "p95_ms":    float(row.get("95%", 0)),
                    "p99_ms":    float(row.get("99%", 0)),
                    "rps":       float(row.get("Requests/s", 0)),
                }
    return None


def load_all_results(results_dir):
    """
    Returns nested dict: data[algo][users] = {requests, failures, avg_ms, ...}
    Skips missing files gracefully.
    """
    data = defaultdict(dict)
    for algo in ALGORITHMS:
        for users in USER_COUNTS:
            path = os.path.join(results_dir, f"{algo}_{users}_stats.csv")
            if os.path.exists(path):
                row = parse_stats_csv(path)
                if row:
                    data[algo][users] = row
    return data


# ─── Plot helpers ─────────────────────────────────────────────────────────────

def style_axes(ax, title, xlabel, ylabel, grid=True):
    ax.set_title(title, fontsize=13, fontweight="bold", pad=10)
    ax.set_xlabel(xlabel, fontsize=11)
    ax.set_ylabel(ylabel, fontsize=11)
    ax.tick_params(labelsize=9)
    if grid:
        ax.grid(True, linestyle="--", alpha=0.5)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)


def save(fig, path):
    fig.tight_layout()
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {path}")


# ─── Individual plots ─────────────────────────────────────────────────────────

def plot_line_metric(data, metric, ylabel, title, filename, out_dir):
    fig, ax = plt.subplots(figsize=(8, 5))
    plotted = False
    for algo in ALGORITHMS:
        if algo not in data:
            continue
        xs = sorted(data[algo].keys())
        ys = [data[algo][u][metric] for u in xs if metric in data[algo].get(u, {})]
        xs_valid = [u for u in xs if metric in data[algo].get(u, {})]
        if not xs_valid:
            continue
        ax.plot(xs_valid, ys,
                label=ALGO_LABELS[algo],
                color=COLORS[algo],
                marker=MARKERS[algo],
                linewidth=2, markersize=7)
        plotted = True

    if not plotted:
        plt.close(fig)
        print(f"  Skipped {filename} — no data")
        return

    ax.set_xticks(USER_COUNTS)
    style_axes(ax, title, "Concurrent Users", ylabel)
    ax.legend(fontsize=9, loc="best")
    save(fig, os.path.join(out_dir, filename))


def plot_failure_heatmap(data, out_dir):
    algos_present = [a for a in ALGORITHMS if a in data and data[a]]
    if not algos_present:
        print("  Skipped failure_rate.png — no data")
        return

    matrix = []
    for algo in algos_present:
        row = []
        for u in USER_COUNTS:
            d = data[algo].get(u)
            if d and d["requests"] > 0:
                row.append(100 * d["failures"] / d["requests"])
            else:
                row.append(float("nan"))
        matrix.append(row)

    matrix = np.array(matrix, dtype=float)
    fig, ax = plt.subplots(figsize=(8, max(3, len(algos_present) * 0.8 + 1)))
    cmap = plt.cm.RdYlGn_r
    cmap.set_bad("lightgrey")
    im = ax.imshow(matrix, cmap=cmap, vmin=0, vmax=max(5, np.nanmax(matrix)),
                   aspect="auto")
    plt.colorbar(im, ax=ax, label="Failure rate (%)")

    ax.set_xticks(range(len(USER_COUNTS)))
    ax.set_xticklabels(USER_COUNTS)
    ax.set_yticks(range(len(algos_present)))
    ax.set_yticklabels([ALGO_LABELS[a] for a in algos_present], fontsize=9)
    ax.set_xlabel("Concurrent Users", fontsize=11)
    ax.set_title("Failure Rate (%) by Algorithm and User Count",
                 fontsize=13, fontweight="bold", pad=10)

    for i, algo in enumerate(algos_present):
        for j, u in enumerate(USER_COUNTS):
            val = matrix[i, j]
            if not math.isnan(val):
                ax.text(j, i, f"{val:.1f}%", ha="center", va="center",
                        fontsize=8, color="black")
    save(fig, os.path.join(out_dir, "failure_rate.png"))


def plot_latency_distribution(data, target_users, out_dir):
    algos_present = [a for a in ALGORITHMS
                     if a in data and target_users in data[a]]
    if not algos_present:
        print(f"  Skipped latency_distribution.png — no data for {target_users} users")
        return

    percentiles = ["p50_ms", "p95_ms", "p99_ms"]
    labels      = ["p50", "p95", "p99"]
    x      = np.arange(len(percentiles))
    width  = 0.8 / len(algos_present)

    fig, ax = plt.subplots(figsize=(9, 5))
    for i, algo in enumerate(algos_present):
        vals = [data[algo][target_users].get(p, 0) for p in percentiles]
        offset = (i - len(algos_present) / 2 + 0.5) * width
        bars = ax.bar(x + offset, vals, width * 0.9,
                      label=ALGO_LABELS[algo],
                      color=COLORS[algo], alpha=0.85)
        for bar, val in zip(bars, vals):
            if val > 0:
                ax.text(bar.get_x() + bar.get_width() / 2,
                        bar.get_height() + 0.5,
                        f"{val:.0f}", ha="center", va="bottom",
                        fontsize=7)

    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=11)
    style_axes(ax,
               f"Latency Distribution at {target_users} Concurrent Users",
               "Percentile", "Response Time (ms)")
    ax.legend(fontsize=9, loc="upper left")
    save(fig, os.path.join(out_dir, "latency_distribution.png"))


def plot_radar(data, target_users, out_dir):
    """Normalised radar chart — higher = better for all axes."""
    algos_present = [a for a in ALGORITHMS
                     if a in data and target_users in data[a]]
    if len(algos_present) < 2:
        print("  Skipped summary_radar.png — need at least 2 algorithms")
        return

    metrics     = ["rps", "avg_ms", "p95_ms", "p99_ms", "failures"]
    metric_lbls = ["RPS", "Avg Latency", "p95 Latency", "p99 Latency", "Failures"]
    # For latency/failures: lower is better → invert before normalising
    invert      = {"avg_ms", "p95_ms", "p99_ms", "failures"}

    raw = {algo: [data[algo][target_users].get(m, 0) for m in metrics]
           for algo in algos_present}

    # Normalise each metric to [0, 1] across algorithms; invert where needed
    norm = {}
    for j, m in enumerate(metrics):
        vals = [raw[a][j] for a in algos_present]
        lo, hi = min(vals), max(vals)
        rng = hi - lo if hi != lo else 1
        for algo in algos_present:
            v = (raw[algo][j] - lo) / rng
            if m in invert:
                v = 1 - v
            norm.setdefault(algo, []).append(v)

    N      = len(metrics)
    angles = [n / float(N) * 2 * math.pi for n in range(N)]
    angles += angles[:1]

    fig, ax = plt.subplots(figsize=(7, 7), subplot_kw=dict(polar=True))
    ax.set_theta_offset(math.pi / 2)
    ax.set_theta_direction(-1)
    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(metric_lbls, fontsize=10)
    ax.set_yticks([0.25, 0.5, 0.75, 1.0])
    ax.set_yticklabels(["25%", "50%", "75%", "100%"], fontsize=7)
    ax.set_ylim(0, 1)

    for algo in algos_present:
        vals = norm[algo] + norm[algo][:1]
        ax.plot(angles, vals, color=COLORS[algo], linewidth=2,
                label=ALGO_LABELS[algo], marker=MARKERS[algo], markersize=5)
        ax.fill(angles, vals, color=COLORS[algo], alpha=0.1)

    ax.set_title(f"Algorithm Comparison ({target_users} users)\n"
                 f"Normalised: outer edge = best",
                 fontsize=12, fontweight="bold", pad=20)
    ax.legend(loc="upper right", bbox_to_anchor=(1.35, 1.15), fontsize=9)
    save(fig, os.path.join(out_dir, "summary_radar.png"))


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Plot SDN load balancer benchmarks")
    parser.add_argument("--dir", default="results",
                        help="Directory containing Locust *_stats.csv files")
    parser.add_argument("--out", default=None,
                        help="Output directory for graphs (default: same as --dir)")
    parser.add_argument("--radar-users", type=int, default=400,
                        help="User count to use for radar/distribution charts")
    args = parser.parse_args()

    results_dir = args.dir
    out_dir     = args.out or results_dir

    os.makedirs(out_dir, exist_ok=True)

    data = load_all_results(results_dir)
    if not data:
        print(f"No *_stats.csv files found in {results_dir}/")
        print("Run: bash run_benchmarks.sh  to collect data first.")
        sys.exit(1)

    total_runs = sum(len(v) for v in data.values())
    print(f"Loaded {total_runs} result files from {results_dir}/")
    print(f"Generating graphs → {out_dir}/\n")

    # 1. Throughput
    plot_line_metric(data, "rps", "Requests / second",
                     "Throughput vs Concurrent Users",
                     "rps_vs_users.png", out_dir)

    # 2. Average latency
    plot_line_metric(data, "avg_ms", "Response time (ms)",
                     "Average Latency vs Concurrent Users",
                     "avg_latency_vs_users.png", out_dir)

    # 3. p95 latency
    plot_line_metric(data, "p95_ms", "Response time (ms)",
                     "p95 Tail Latency vs Concurrent Users",
                     "p95_latency_vs_users.png", out_dir)

    # 4. p99 latency
    plot_line_metric(data, "p99_ms", "Response time (ms)",
                     "p99 Tail Latency vs Concurrent Users",
                     "p99_latency_vs_users.png", out_dir)

    # 5. Failure rate heatmap
    plot_failure_heatmap(data, out_dir)

    # 6. Latency distribution bar chart
    plot_latency_distribution(data, args.radar_users, out_dir)

    # 7. Radar chart
    plot_radar(data, args.radar_users, out_dir)

    print(f"\nAll graphs saved to {out_dir}/")


if __name__ == "__main__":
    main()
