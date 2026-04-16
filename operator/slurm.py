"""Slurm REST API client (slurmrestd).

Provides HTTP access to slurmrestd with optional HS256 JWT authentication.
Falls back gracefully — callers check ping() and switch to kubectl exec when
slurmrestd is unreachable.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.request


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class SlurmRestClient:
    """HTTP client for slurmrestd.

    slurmrestd must be started in the controller pod with:
        /usr/sbin/slurmrestd -a rest_auth/local 0.0.0.0:6820
    and exposed via the slurm-restapi ClusterIP Service on port 6820.

    auth/local trusts the X-SLURM-USER-NAME header — no secrets required.
    Uses stdlib urllib only; no extra Python dependencies.
    """

    _BUSY_STATES = {"allocated", "mixed", "completing"}

    def __init__(self, base_url: str, api_version: str = "v0.0.37",
                 username: str = "root", timeout: int = 10,
                 jwt_key_path: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_version = api_version
        self.username = username
        self.timeout = timeout
        self._jwt_key: bytes | None = None
        if jwt_key_path:
            try:
                with open(jwt_key_path, "rb") as fh:
                    self._jwt_key = fh.read()
            except OSError:
                pass  # ping() will fail → caller falls back to exec

    def _make_token(self, lifetime: int = 3600) -> str:
        """Generate a HS256 JWT token accepted by slurmrestd -a rest_auth/jwt."""
        now = int(time.time())
        header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        payload = _b64url(json.dumps({
            "exp": now + lifetime,
            "iat": now,
            "iss": "slurmrestd",
            "sun": self.username,
        }).encode())
        signing_input = f"{header}.{payload}"
        sig = _b64url(hmac.new(self._jwt_key, signing_input.encode(), hashlib.sha256).digest())
        return f"{signing_input}.{sig}"

    def _get(self, path: str, *, retries: int = 3, backoff: float = 1.0) -> dict:
        url = f"{self.base_url}{path}"
        last_exc: Exception = RuntimeError("no attempts made")
        for attempt in range(retries):
            if attempt:
                time.sleep(backoff * (2 ** (attempt - 1)))  # 1 s, 2 s
            try:
                req = urllib.request.Request(url)
                req.add_header("X-SLURM-USER-NAME", self.username)
                if self._jwt_key is not None:
                    req.add_header("X-SLURM-USER-TOKEN", self._make_token())
                req.add_header("Accept", "application/json")
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode())
            except Exception as exc:
                last_exc = exc
        raise last_exc

    def ping(self) -> bool:
        try:
            self._get(f"/slurm/{self.api_version}/diag")
            return True
        except Exception:
            return False

    def list_jobs(self, partition: str) -> list[dict]:
        """Return all PENDING and RUNNING jobs in the given partition."""
        data = self._get(f"/slurm/{self.api_version}/jobs")
        return [
            j for j in data.get("jobs", [])
            if j.get("partition") == partition
            and j.get("job_state") in ("PENDING", "RUNNING")
        ]

    def list_nodes(self) -> list[dict]:
        data = self._get(f"/slurm/{self.api_version}/nodes")
        return data.get("nodes", [])

    @staticmethod
    def _node_states(node: dict) -> set[str]:
        """Normalise state field — can be a list or a single string."""
        raw = node.get("state", "")
        if isinstance(raw, list):
            return {s.lower() for s in raw}
        return {raw.lower()} if raw else set()

    @staticmethod
    def _normalize_job(job: dict) -> dict[str, str]:
        """Map REST API fields to the same dict shape used by exec-based parsing."""
        features_raw = job.get("features", "")
        features = ",".join(features_raw) if isinstance(features_raw, list) else str(features_raw or "")

        tres = job.get("tres_per_node", "") or ""
        if tres in ("N/A", "none", "None"):
            tres = ""

        return {
            "NodeList": job.get("nodes", "(null)") or "(null)",
            "Features": features,
            "TresPerNode": tres,
        }
