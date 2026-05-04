#!/bin/bash
# run_benchmarks.sh — automated benchmark sweep across all algorithms and user counts
#
# Prerequisites:
#   - Mininet topology running (sudo /usr/bin/python3 topology.py)
#   - Run this script from a separate terminal (not inside the Mininet CLI)
#
# Usage:
#   bash run_benchmarks.sh
#   bash run_benchmarks.sh --duration 120 --users "100 200 400"

set -euo pipefail

# ─── Configuration ────────────────────────────────────────────────────────────
ALGORITHMS="round_robin random ip_hash least_connections weighted"
USER_COUNTS="100 200 400 600 800"
SPAWN_RATE=20          # users spawned per second
DURATION=60            # seconds per run
RESULTS_DIR="$(cd "$(dirname "$0")/results" && pwd)"
LB_FILE="$(cd "$(dirname "$0")" && pwd)/load_balancer.py"
LOCUST_BIN="$HOME/ryu-env/bin/locust"
LOCUSTFILE="$(cd "$(dirname "$0")/benchmark" && pwd)/locustfile.py"
RYU_BIN="$HOME/ryu-env/bin/ryu-manager"

# Parse optional flags
while [[ $# -gt 0 ]]; do
    case $1 in
        --duration) DURATION="$2"; shift 2 ;;
        --users)    USER_COUNTS="$2"; shift 2 ;;
        *) echo "Unknown flag: $1"; exit 1 ;;
    esac
done

# ─── Helpers ──────────────────────────────────────────────────────────────────

log() { echo "[$(date '+%H:%M:%S')] $*"; }

die() { echo "ERROR: $*" >&2; exit 1; }

# Find the PID of a Mininet host's bash process (for nsenter)
host_pid() {
    pgrep -f "mininet:$1" | head -1
}

# Wait until the OVS switch has an ESTABLISHED connection to RYU on 6633
wait_for_switch() {
    local timeout=30
    log "Waiting for switch to connect to RYU..."
    for i in $(seq 1 $timeout); do
        if ss -tnp 2>/dev/null | grep -q "6633.*ESTAB\|ESTAB.*6633"; then
            log "Switch connected (${i}s)"
            return 0
        fi
        sleep 1
    done
    die "Switch did not connect within ${timeout}s"
}

# Set the ALGORITHM variable in load_balancer.py
set_algorithm() {
    sed -i "s/^ALGORITHM = .*/ALGORITHM = \"$1\"/" "$LB_FILE"
    log "Algorithm set to: $1"
}

# Start ryu-manager in background, return its PID
start_ryu() {
    source "$HOME/ryu-env/bin/activate"
    "$RYU_BIN" "$LB_FILE" > "/tmp/ryu_$1.log" 2>&1 &
    echo $!
}

# ─── Preflight checks ─────────────────────────────────────────────────────────

log "=== SDN Load Balancer Benchmark Suite ==="
log "Algorithms : $ALGORITHMS"
log "User counts: $USER_COUNTS"
log "Duration   : ${DURATION}s per run"
log "Results dir: $RESULTS_DIR"
echo

[[ -f "$LB_FILE" ]]     || die "load_balancer.py not found at $LB_FILE"
[[ -f "$LOCUSTFILE" ]]  || die "locustfile.py not found at $LOCUSTFILE"
[[ -x "$LOCUST_BIN" ]]  || die "locust not found at $LOCUST_BIN"
[[ -x "$RYU_BIN" ]]     || die "ryu-manager not found at $RYU_BIN"

H1_PID=$(host_pid h1 2>/dev/null) || die "Mininet host h1 not found — is the topology running?"
log "Found h1 namespace (PID $H1_PID)"

mkdir -p "$RESULTS_DIR"

# ─── Main sweep ───────────────────────────────────────────────────────────────

TOTAL=$(echo $ALGORITHMS | wc -w)
TOTAL=$((TOTAL * $(echo $USER_COUNTS | wc -w)))
RUN=0

for algo in $ALGORITHMS; do
    log "━━━ Algorithm: $algo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    # Kill any existing ryu-manager
    pkill -f "ryu-manager" 2>/dev/null || true
    sleep 1

    set_algorithm "$algo"
    RYU_PID=$(start_ryu "$algo")
    log "ryu-manager started (PID $RYU_PID, log: /tmp/ryu_${algo}.log)"

    wait_for_switch
    sleep 2   # let table-miss rule propagate

    for users in $USER_COUNTS; do
        RUN=$((RUN + 1))
        CSV_PREFIX="$RESULTS_DIR/${algo}_${users}"
        log "Run $RUN/$TOTAL — $algo / $users users → $CSV_PREFIX"

        # Run Locust inside h1's network namespace so it can reach 10.0.0.100
        sudo nsenter --net="/proc/$H1_PID/ns/net" -- \
            "$LOCUST_BIN" \
            -f "$LOCUSTFILE" \
            --headless \
            -u "$users" \
            -r "$SPAWN_RATE" \
            -t "${DURATION}s" \
            --csv="$CSV_PREFIX" \
            --csv-full-history \
            2>&1 | grep -E "starting|stopping|Aggregated|ERROR" || true

        log "Done → ${CSV_PREFIX}_stats.csv"
        sleep 3   # brief pause between runs
    done

    # Clean up ryu for this algorithm
    kill "$RYU_PID" 2>/dev/null || true
    sleep 1
done

# Restore default algorithm
set_algorithm "round_robin"

log ""
log "=== Sweep complete ==="
log "Results saved to: $RESULTS_DIR"
log "Generate graphs : python3 $(dirname $LB_FILE)/plot_results.py"
