"""
Slurm REST API → Prometheus exporter.

Scrapes slurmrestd via HTTP and exposes metrics on METRICS_PORT (default 9341).
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.request

from prometheus_client import Gauge, start_http_server

# ---------------------------------------------------------------------------
# Configuration from environment variables
# ---------------------------------------------------------------------------
SLURM_REST_URL: str = os.environ.get(
    "SLURM_REST_URL",
    "http://slurm-restapi.slurm.svc.cluster.local:6820",
).rstrip("/")
SLURM_REST_API_VERSION: str = os.environ.get("SLURM_REST_API_VERSION", "v0.0.37")
SLURM_JWT_KEY_PATH: str = os.environ.get("SLURM_JWT_KEY_PATH", "")
SCRAPE_INTERVAL_SECONDS: int = int(os.environ.get("SCRAPE_INTERVAL_SECONDS", "15"))
METRICS_PORT: int = int(os.environ.get("METRICS_PORT", "9341"))

# Node state classification sets (compared in lowercase)
_BUSY_STATES: frozenset[str] = frozenset({"allocated", "mixed", "completing"})
_IDLE_STATES: frozenset[str] = frozenset({"idle"})
_DOWN_STATES: frozenset[str] = frozenset({"down", "drain", "not_responding"})

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
QUEUE_PENDING = Gauge("slurm_queue_pending", "Number of PENDING Slurm jobs")
QUEUE_RUNNING = Gauge("slurm_queue_running", "Number of RUNNING Slurm jobs")
NODES_IDLE = Gauge("slurm_nodes_idle", "Number of idle Slurm nodes")
NODES_ALLOC = Gauge("slurm_nodes_alloc", "Number of allocated/mixed Slurm nodes")
NODES_DOWN = Gauge("slurm_nodes_down", "Number of down/drain/not-responding Slurm nodes")
NODES_DRAINING = Gauge("slurm_nodes_draining", "Number of nodes in DRAIN state (accepting no new jobs)")
NODES_TOTAL = Gauge("slurm_nodes_total", "Total number of Slurm nodes registered")
SCRAPE_SUCCESS = Gauge("slurm_exporter_scrape_success", "1 if the last scrape succeeded, 0 on error")
QUEUE_OLDEST_WAIT = Gauge(
    "slurm_job_queue_oldest_wait_seconds",
    "Seconds since the oldest PENDING job was submitted (0 if queue is empty)",
)
QUEUE_AVG_WAIT = Gauge(
    "slurm_job_queue_avg_wait_seconds",
    "Mean wait time of all currently PENDING jobs in seconds (0 if none)",
)
# Scheduler diagnostics (/diag endpoint)
SCHED_CYCLE_LAST = Gauge(
    "slurm_scheduler_cycle_last_seconds",
    "Duration of the last Slurm scheduler cycle in seconds",
)
SCHED_CYCLE_MEAN = Gauge(
    "slurm_scheduler_cycle_mean_seconds",
    "Mean Slurm scheduler cycle duration in seconds",
)
BACKFILL_CYCLE_LAST = Gauge(
    "slurm_backfill_cycle_last_seconds",
    "Duration of the last backfill scheduling cycle in seconds",
)
BACKFILL_QUEUE_LENGTH = Gauge(
    "slurm_backfill_queue_length",
    "Number of jobs considered by the backfill scheduler in the last cycle",
)
BACKFILL_DEPTH_TRY_LAST = Gauge(
    "slurm_backfill_depth_try_last",
    "Backfill search depth reached in the last cycle",
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("slurm_exporter")


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------
def _b64url(data: bytes) -> str:
    """Return URL-safe base64 encoding with no padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _make_jwt_token(key: bytes, username: str, lifetime: int = 3600) -> str:
    """
    Generate a minimal HS256 JWT token for slurmrestd authentication.

    Args:
        key:      Raw HMAC-SHA256 signing key bytes.
        username: Slurm username to embed in the token (typically "root").
        lifetime: Token validity in seconds (default 3600).

    Returns:
        A signed JWT string.
    """
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    now = int(time.time())
    payload = _b64url(
        json.dumps(
            {
                "exp": now + lifetime,
                "iat": now,
                "sun": username,
            }
        ).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    sig = hmac.new(key, signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url(sig)}"


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------
def _http_get(url: str, jwt_key: bytes | None) -> dict:
    """
    Perform an authenticated HTTP GET against slurmrestd and return parsed JSON.

    Args:
        url:     Full URL to fetch.
        jwt_key: HMAC key bytes for JWT generation, or None to use header-only auth.

    Returns:
        Parsed JSON response as a dict.

    Raises:
        urllib.error.URLError: On network or HTTP errors.
        ValueError:            If the response body is not valid JSON.
    """
    headers = {
        "X-SLURM-USER-NAME": "root",
        "Accept": "application/json",
    }
    if jwt_key is not None:
        token = _make_jwt_token(jwt_key, username="root")
        headers["X-SLURM-USER-TOKEN"] = token

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read()
    return json.loads(body)


# ---------------------------------------------------------------------------
# Scrape logic
# ---------------------------------------------------------------------------
def scrape(jwt_key: bytes | None) -> None:
    """
    Scrape slurmrestd for job and node metrics and update Prometheus gauges.

    Args:
        jwt_key: HMAC key bytes for JWT auth, or None to fall back to header-only auth.
    """
    base = f"{SLURM_REST_URL}/slurm/{SLURM_REST_API_VERSION}"

    # --- Jobs ---
    jobs_data = _http_get(f"{base}/jobs", jwt_key)
    jobs = jobs_data.get("jobs", [])

    pending = 0
    running = 0
    now_ts = time.time()
    wait_times: list[float] = []

    for job in jobs:
        raw_state = job.get("job_state", "")
        # job_state may be a list or a plain string depending on API version
        if isinstance(raw_state, list):
            states = [s.upper() for s in raw_state]
        else:
            states = [raw_state.upper()]

        if "PENDING" in states:
            pending += 1
            # Scheduling overhead: time since job was submitted
            # submit_time is an integer epoch seconds; newer API versions wrap it as {"number": N}
            st = job.get("submit_time", 0)
            if isinstance(st, dict):
                st = st.get("number", 0)
            try:
                st = int(st or 0)
            except (TypeError, ValueError):
                st = 0
            if st > 0:
                wait_times.append(now_ts - st)
        elif "RUNNING" in states:
            running += 1

    QUEUE_PENDING.set(pending)
    QUEUE_RUNNING.set(running)
    if wait_times:
        QUEUE_OLDEST_WAIT.set(max(wait_times))
        QUEUE_AVG_WAIT.set(sum(wait_times) / len(wait_times))
    else:
        QUEUE_OLDEST_WAIT.set(0)
        QUEUE_AVG_WAIT.set(0)
    log.info("jobs scraped: pending=%d running=%d oldest_wait=%.1fs",
             pending, running, max(wait_times) if wait_times else 0.0)

    # --- Nodes ---
    nodes_data = _http_get(f"{base}/nodes", jwt_key)
    nodes = nodes_data.get("nodes", [])

    idle = alloc = down = draining = 0
    for node in nodes:
        raw_state = node.get("state", "")
        # state may be a list or a plain string
        if isinstance(raw_state, list):
            state_tokens = {s.lower() for s in raw_state}
        else:
            state_tokens = {s.lower() for s in raw_state.split()}

        if state_tokens & _BUSY_STATES:
            alloc += 1
        elif state_tokens & _IDLE_STATES:
            idle += 1
        elif state_tokens & _DOWN_STATES:
            down += 1

        # DRAIN is an overlay flag: node accepts no new jobs but may still be running some.
        # It can co-exist with idle/alloc/down states, so we track it separately.
        if "drain" in state_tokens:
            draining += 1

    total = len(nodes)
    NODES_IDLE.set(idle)
    NODES_ALLOC.set(alloc)
    NODES_DOWN.set(down)
    NODES_DRAINING.set(draining)
    NODES_TOTAL.set(total)
    log.info(
        "nodes scraped: total=%d idle=%d alloc=%d down=%d draining=%d",
        total, idle, alloc, down, draining,
    )

    SCRAPE_SUCCESS.set(1)

    # --- Scheduler diagnostics (sdiag) ---
    # Non-fatal: older slurmrestd builds may not support /diag or return different fields.
    def _us_to_s(v: object) -> float:
        """Convert microseconds (possibly dict-wrapped) to seconds."""
        if isinstance(v, dict):
            v = v.get("number", 0)
        try:
            return float(v or 0) / 1_000_000
        except (TypeError, ValueError):
            return 0.0

    def _int_field(v: object) -> int:
        if isinstance(v, dict):
            v = v.get("number", 0)
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    try:
        diag_data = _http_get(f"{base}/diag", jwt_key)
        # Slurm ≥ 22.05 wraps stats under "statistics"; older versions may use top-level keys
        stats = diag_data.get("statistics", diag_data)
        SCHED_CYCLE_LAST.set(_us_to_s(
            stats.get("schedule_cycle_last") or stats.get("last_cycle", 0)))
        SCHED_CYCLE_MEAN.set(_us_to_s(
            stats.get("schedule_cycle_mean") or stats.get("mean_cycle", 0)))
        BACKFILL_CYCLE_LAST.set(_us_to_s(
            stats.get("backfill_last_cycle_time") or stats.get("backfill_last_cycle", 0)))
        BACKFILL_QUEUE_LENGTH.set(_int_field(stats.get("backfill_queue_length", 0)))
        BACKFILL_DEPTH_TRY_LAST.set(_int_field(
            stats.get("backfill_last_depth_try") or stats.get("bf_last_depth_try", 0)))
        log.info(
            "diag scraped: sched_cycle_last=%.3fs backfill_queue=%d",
            SCHED_CYCLE_LAST._value.get(), _int_field(stats.get("backfill_queue_length", 0)),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("diag scrape failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    """Read configuration, start the metrics HTTP server, and run the scrape loop."""
    log.info(
        "slurm-exporter starting: url=%s api_version=%s port=%d interval=%ds",
        SLURM_REST_URL,
        SLURM_REST_API_VERSION,
        METRICS_PORT,
        SCRAPE_INTERVAL_SECONDS,
    )

    # Resolve JWT key once at startup
    jwt_key: bytes | None = None
    if SLURM_JWT_KEY_PATH:
        try:
            with open(SLURM_JWT_KEY_PATH, "rb") as fh:
                jwt_key = fh.read().strip()
            log.info("JWT key loaded from %s", SLURM_JWT_KEY_PATH)
        except OSError as exc:
            log.warning(
                "Cannot read JWT key from %s (%s); falling back to header-only auth",
                SLURM_JWT_KEY_PATH,
                exc,
            )
    else:
        log.info("SLURM_JWT_KEY_PATH not set; using header-only auth (X-SLURM-USER-NAME)")

    start_http_server(METRICS_PORT)
    log.info("metrics server listening on :%d", METRICS_PORT)

    while True:
        try:
            scrape(jwt_key)
        except Exception as exc:  # noqa: BLE001
            SCRAPE_SUCCESS.set(0)
            log.warning("scrape failed: %s", exc, exc_info=True)

        time.sleep(SCRAPE_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
