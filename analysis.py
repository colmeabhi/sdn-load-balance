"""
analysis.py — Parse Locust CSV results and print a comparison table.

Usage:
    python3 analysis.py results/          # reads all *_stats.csv files
    python3 analysis.py results/rr_100_stats.csv results/lc_100_stats.csv
"""

import sys
import os
import csv
import glob
from pathlib import Path


def parse_locust_stats_csv(path):
    """Return the aggregated row from a Locust *_stats.csv file."""
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("Name") == "Aggregated":
                return row
    return None


def load_results(paths):
    rows = []
    for p in sorted(paths):
        row = parse_locust_stats_csv(p)
        if row is None:
            print(f"  [skip] no Aggregated row in {p}")
            continue
        rows.append({
            "file":    Path(p).stem,
            "reqs":    int(row.get("Request Count", 0)),
            "fails":   int(row.get("Failure Count", 0)),
            "avg_ms":  float(row.get("Average Response Time", 0)),
            "p50_ms":  float(row.get("50%", 0)),
            "p95_ms":  float(row.get("95%", 0)),
            "p99_ms":  float(row.get("99%", 0)),
            "rps":     float(row.get("Requests/s", 0)),
        })
    return rows


def print_table(rows):
    if not rows:
        print("No results found.")
        return

    hdr = f"{'File':<35} {'Reqs':>7} {'Fails':>6} {'Avg ms':>8} {'p50':>7} {'p95':>7} {'p99':>7} {'RPS':>8}"
    print("\n" + hdr)
    print("-" * len(hdr))
    for r in rows:
        fail_pct = 100 * r["fails"] / r["reqs"] if r["reqs"] else 0
        print(
            f"{r['file']:<35} {r['reqs']:>7} {r['fails']:>5} ({fail_pct:4.1f}%)"
            f" {r['avg_ms']:>8.1f} {r['p50_ms']:>7.0f} {r['p95_ms']:>7.0f}"
            f" {r['p99_ms']:>7.0f} {r['rps']:>8.1f}"
        )
    print()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    paths = []
    for arg in sys.argv[1:]:
        if os.path.isdir(arg):
            paths.extend(glob.glob(os.path.join(arg, "*_stats.csv")))
        else:
            paths.append(arg)

    if not paths:
        print("No CSV files found.")
        sys.exit(1)

    rows = load_results(paths)
    print_table(rows)


if __name__ == "__main__":
    main()
