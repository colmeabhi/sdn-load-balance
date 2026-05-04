# sdn-load-balance

Swapping and benchmarking load balancing algorithms in software-defined networks using RYU (OpenFlow 1.3) and Mininet.

## What it does

Clients send HTTP requests to a single Virtual IP (`10.0.0.100`). An SDN controller intercepts each new connection, picks a backend server using one of five algorithms, and installs rewrite rules directly into the switch. Subsequent packets in the same flow are handled at line rate with no controller involvement.

## Repo structure

```
sdn-load-balance/
├── topology.py            # Mininet network: 3 clients + 4 servers + 1 OVS switch
├── load_balancer.py       # RYU controller app — change ALGORITHM here
├── run_benchmarks.sh      # Automated sweep: all 5 algorithms × 5 user counts
├── plot_results.py        # Generates 7 comparison graphs from benchmark CSVs
├── analysis.py            # Prints comparison table from benchmark CSVs
├── benchmark/
│   └── locustfile.py      # Locust HTTP load test targeting the VIP
├── results/               # Benchmark CSVs and graphs land here (git-ignored)
├── requirements.txt       # Pinned Python deps for the ryu-env
└── PROJECT.md             # Full theory, code walkthrough, and packet traces
```

## Quick start

**Terminal 1 — start the controller:**
```bash
source ~/ryu-env/bin/activate
ryu-manager load_balancer.py
```

**Terminal 2 — start the network:**
```bash
sudo /usr/bin/python3 topology.py
```

**Verify it works:**
```
mininet> pingall
mininet> h1 curl -s http://10.0.0.100
```

## Switching algorithms

Open `load_balancer.py` and change line 32:
```python
ALGORITHM = "round_robin"   # round_robin | random | ip_hash | least_connections | weighted
```
Restart `ryu-manager` to apply.

## Running benchmarks

**Automated sweep** (all 5 algorithms × 5 user counts = 25 runs, from a separate terminal while Mininet is running):
```bash
bash run_benchmarks.sh
```

**Generate graphs** after the sweep:
```bash
~/ryu-env/bin/python plot_results.py
# graphs saved to results/
```

Generates 7 plots: throughput, avg/p95/p99 latency, failure rate heatmap, latency distribution bar chart, and a normalised radar chart.

**Manual single run** (from inside the Mininet CLI):
```
mininet> h1 ~/ryu-env/bin/locust -f benchmark/locustfile.py \
    --headless -u 100 -r 10 -t 60s --csv=results/rr_100
```

**Compare results as a table:**
```bash
~/ryu-env/bin/python analysis.py results/
```

## Deep dive

See **[PROJECT.md](PROJECT.md)** for SDN/OpenFlow theory, a step-by-step packet trace, explanation of every design decision in the code, and a full benchmarking guide.
