import time
from threading import Lock


class InMemoryMetrics:
    def __init__(self) -> None:
        self._started_at = time.time()
        self._lock = Lock()
        self._total_requests = 0
        self._requests_by_path: dict[str, int] = {}
        self._responses_by_status: dict[str, int] = {}
        self._backend_calls = 0
        self._backend_errors = 0
        self._backend_timeouts = 0
        self._total_request_duration_ms = 0.0

    def record_request(self, path: str) -> None:
        with self._lock:
            self._total_requests += 1
            self._requests_by_path[path] = self._requests_by_path.get(path, 0) + 1

    def record_response(self, status_code: int, duration_ms: float) -> None:
        with self._lock:
            status_key = str(status_code)
            self._responses_by_status[status_key] = self._responses_by_status.get(status_key, 0) + 1
            self._total_request_duration_ms += duration_ms

    def record_backend_call(self) -> None:
        with self._lock:
            self._backend_calls += 1

    def record_backend_error(self) -> None:
        with self._lock:
            self._backend_errors += 1

    def record_backend_timeout(self) -> None:
        with self._lock:
            self._backend_timeouts += 1

    def snapshot(self) -> dict:
        with self._lock:
            total_requests = self._total_requests
            average_duration_ms = (
                round(self._total_request_duration_ms / total_requests, 2) if total_requests else 0.0
            )
            return {
                "uptime_seconds": round(time.time() - self._started_at, 2),
                "total_requests": total_requests,
                "requests_by_path": dict(sorted(self._requests_by_path.items())),
                "responses_by_status": dict(sorted(self._responses_by_status.items())),
                "backend_calls": self._backend_calls,
                "backend_errors": self._backend_errors,
                "backend_timeouts": self._backend_timeouts,
                "average_request_duration_ms": average_duration_ms,
            }


metrics = InMemoryMetrics()
