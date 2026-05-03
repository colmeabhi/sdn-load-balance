"""
benchmark/locustfile.py — Locust load test for the SDN load balancer

Simulates HTTP clients hitting the virtual IP (10.0.0.100).
The RYU controller transparently redirects each client to a backend server.

Run (from inside the Mininet h1 shell or any host that can reach 10.0.0.100):

    # Headless, CSV output, 100 users, 10 spawn rate, 60s duration:
    locust -f benchmark/locustfile.py \
        --headless -u 100 -r 10 -t 60s \
        --csv=results/rr_100

    # Repeat for other user counts:
    for users in 100 200 400 600 800; do
        locust -f benchmark/locustfile.py \
            --headless -u $users -r 20 -t 60s \
            --csv=results/rr_$users
    done
"""

from locust import HttpUser, task, between, events
import time
import csv
import os


# ─── Target ──────────────────────────────────────────────────────────────────
# Matches the VIRTUAL_IP in load_balancer.py
HOST = "http://10.0.0.100"


# ─── User behaviour ───────────────────────────────────────────────────────────

class WebClient(HttpUser):
    """Simulates a browser-like client hitting the virtual IP."""
    host      = HOST
    wait_time = between(0.5, 2)   # seconds between requests per user

    @task(3)
    def get_index(self):
        """Most common request — fetch the root page."""
        self.client.get("/", name="GET /index")

    @task(1)
    def get_health(self):
        """Lightweight health-check endpoint."""
        self.client.get("/index.html", name="GET /index.html")


# ─── Result collection hook ───────────────────────────────────────────────────

@events.quitting.add_listener
def on_quitting(environment, **kwargs):
    """Write a one-row summary CSV when the test ends."""
    os.makedirs("results", exist_ok=True)
    stats   = environment.stats.total
    ts      = int(time.time())
    out     = f"results/summary_{ts}.csv"

    with open(out, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "timestamp", "num_users", "requests", "failures",
            "avg_response_ms", "p50_ms", "p95_ms", "p99_ms", "rps",
        ])
        writer.writerow([
            ts,
            environment.runner.user_count if environment.runner else "?",
            stats.num_requests,
            stats.num_failures,
            round(stats.avg_response_time, 1),
            stats.get_response_time_percentile(0.50),
            stats.get_response_time_percentile(0.95),
            stats.get_response_time_percentile(0.99),
            round(stats.current_rps, 2),
        ])
    print(f"\n[locust] Summary written to {out}")
