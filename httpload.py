#!/usr/bin/env python3
"""
httpload — HTTP load testing tool

Fixed Worker Pool pattern using concurrent.futures.ThreadPoolExecutor.
Uses requests.Session for connection pooling (HTTP keep-alive).

Usage:
  python httpload.py --url http://localhost:8080/ --requests 1000 --concurrency 20 --timeout 2s
"""

import argparse
import signal
import sys
import time
import threading

import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# CLI & Validation
# ---------------------------------------------------------------------------

def parse_timeout(raw: str) -> float:
    """Parse a timeout string like '2s', '500ms', '1.5s' into seconds (float)."""
    raw = raw.strip().lower()
    if raw.endswith("ms"):
        return float(raw[:-2]) / 1000.0
    elif raw.endswith("s"):
        return float(raw[:-1])
    else:
        # Try bare number (interpret as seconds)
        return float(raw)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="HTTP load testing tool — send N concurrent GET requests and report stats."
    )
    parser.add_argument("-u", "--url", required=True, help="Target HTTP URL to benchmark")
    parser.add_argument(
        "-n", "--requests", type=int, required=True, help="Total number of requests to send"
    )
    parser.add_argument(
        "-c", "--concurrency", type=int, required=True, help="Maximum concurrent in-flight requests"
    )
    parser.add_argument(
        "-t", "--timeout", required=True, help="Per-request timeout (e.g. 2s, 500ms)"
    )
    return parser


def validate_args(args) -> float:
    """Validate CLI arguments. Returns timeout in seconds. Exits on error."""
    errors = []

    # URL
    if not args.url:
        errors.append("--url is required")
    else:
        parsed = urlparse(args.url)
        if parsed.scheme not in ("http", "https"):
            errors.append(f"URL scheme must be http or https, got: {args.url}")
        if not parsed.netloc:
            errors.append(f"Invalid URL (missing host): {args.url}")

    # requests
    if args.requests <= 0:
        errors.append(f"--requests must be > 0, got {args.requests}")

    # concurrency
    if args.concurrency <= 0:
        errors.append(f"--concurrency must be > 0, got {args.concurrency}")

    # timeout
    try:
        timeout_sec = parse_timeout(args.timeout)
        if timeout_sec <= 0:
            errors.append(f"--timeout must be > 0, got {timeout_sec}s")
    except (ValueError, TypeError):
        errors.append(f"Invalid --timeout format: {args.timeout} (expected e.g. 2s, 500ms)")

    if errors:
        for e in errors:
            print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    return timeout_sec


# ---------------------------------------------------------------------------
# Statistics (thread-safe)
# ---------------------------------------------------------------------------

class Stats:
    """Thread-safe statistics collector.

    ``_completed`` is an independent counter incremented once per finished
    request.  Conservation is checked as::

        _completed == succeeded + non2xx + errors + timeouts

    This is NOT a tautology — a code path that double-counts or omits a
    category would break the invariant.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._completed = 0
        self.succeeded = 0
        self.non2xx = 0
        self.errors = 0
        self.timeouts = 0
        self.latencies = []             # list of float (seconds)
        # In-flight tracking
        self._in_flight = 0
        self.max_in_flight = 0

    # -- lifecycle: one call per request ---------------------------------------

    def enter_request(self):
        """Increment in-flight counter (called before HTTP call)."""
        with self._lock:
            self._in_flight += 1
            if self._in_flight > self.max_in_flight:
                self.max_in_flight = self._in_flight

    def exit_request(self, category: str, latency: float):
        """Record completed request in *category* with *latency* (seconds).

        *category* must be one of ``succeeded | non2xx | errors | timeouts``.
        """
        with self._lock:
            self._completed += 1
            self._in_flight -= 1
            if category == "succeeded":
                self.succeeded += 1
            elif category == "non2xx":
                self.non2xx += 1
            elif category == "errors":
                self.errors += 1
            elif category == "timeouts":
                self.timeouts += 1
            else:
                raise ValueError(f"Unknown category: {category}")
            self.latencies.append(latency)

    # -- derived ---------------------------------------------------------------

    @property
    def completed(self) -> int:
        return self._completed

    def conservation_ok(self) -> bool:
        """Return True iff the independent counter matches the category sum.

        Unlike the previous version (where ``completed`` was *defined* as the
        sum), ``_completed`` is incremented independently per request.  A bug
        that double-categorises or skips a category will make this return
        False.
        """
        return self._completed == (
            self.succeeded + self.non2xx + self.errors + self.timeouts
        )


# ---------------------------------------------------------------------------
# Single HTTP request
# ---------------------------------------------------------------------------

def do_request(session: requests.Session, url: str, timeout_sec: float,
               stats: Stats) -> None:
    """
    Execute one HTTP GET request and record the outcome in *stats*.

    Uses ``stream=True`` to avoid buffering the entire response body in
    memory, then discards chunks immediately (题目要求: 不在内存中长期保存
    完整响应正文).
    """
    stats.enter_request()
    start = time.monotonic()

    try:
        # stream=True → body is read lazily in iter_content chunks
        resp = session.get(url, timeout=timeout_sec, stream=True)

        # Discard body chunk-by-chunk; never hold the full body in memory
        for _ in resp.iter_content(chunk_size=8192):
            pass
        resp.close()

        latency = time.monotonic() - start
        if 200 <= resp.status_code < 300:
            stats.exit_request("succeeded", latency)
        else:
            stats.exit_request("non2xx", latency)

    except requests.exceptions.ConnectionError:
        # ConnectTimeout inherits from BOTH ConnectionError and Timeout
        # (MRO: ConnectTimeout → ConnectionError → Timeout).
        # Catching ConnectionError FIRST ensures all connection failures
        # (including Windows ConnectTimeout on dead ports) → Error.
        stats.exit_request("errors", time.monotonic() - start)

    except requests.exceptions.Timeout:
        # Only reached by ReadTimeout now (ConnectTimeout caught above).
        stats.exit_request("timeouts", time.monotonic() - start)

    except requests.exceptions.RequestException:
        stats.exit_request("errors", time.monotonic() - start)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_results(url: str, requested: int, concurrency: int, stats: Stats,
                  elapsed: float, interrupted: bool, scheduled: int) -> None:
    """Print the benchmark report to stdout."""
    print()
    print(f"Target:         {url}")
    print(f"Requests:       {requested}")
    print(f"Concurrency:    {concurrency}")
    print()

    if interrupted:
        print(f"Interrupted:    true")
        print(f"Scheduled:      {scheduled}")

    print(f"Completed:      {stats.completed}")
    print(f"Succeeded:      {stats.succeeded}")
    print(f"Non-2xx:        {stats.non2xx}")
    print(f"Errors:         {stats.errors}")
    print(f"Timeouts:       {stats.timeouts}")
    print()
    print(f"Elapsed:        {elapsed:.2f}s")
    if elapsed > 0:
        print(f"Requests/sec:   {stats.completed / elapsed:.2f}")

    if stats.latencies:
        lat = sorted(stats.latencies)
        avg_ms = (sum(lat) / len(lat)) * 1000
        min_ms = lat[0] * 1000
        max_ms = lat[-1] * 1000
        print(f"Avg latency:    {avg_ms:.2f}ms")
        print(f"Min latency:    {min_ms:.2f}ms")
        print(f"Max latency:    {max_ms:.2f}ms")
    else:
        print("Avg latency:    N/A")
        print("Min latency:    N/A")
        print("Max latency:    N/A")

    # Conservation check — _completed is an independent counter, so this
    # catches real implementation bugs (double-count / skipped category).
    if not stats.conservation_ok():
        print()
        print("[WARN] Stats conservation check FAILED — "
              "Completed != Succeeded + Non-2xx + Errors + Timeouts",
              file=sys.stderr)


# ---------------------------------------------------------------------------
# Core runner
# ---------------------------------------------------------------------------

def run_load_test(url: str, num_requests: int, concurrency: int,
                  timeout_sec: float, ext_interrupt: threading.Event = None) -> tuple:
    """
    Run the load test.

    If *ext_interrupt* is provided the caller controls cancellation (used in
    tests).  Otherwise a SIGINT handler is installed.

    Returns (stats, elapsed, interrupted, scheduled, concurrency).
    """
    # Auto-adjust concurrency if it exceeds total requests
    if concurrency > num_requests:
        concurrency = num_requests

    stats = Stats()
    original_handler = None

    if ext_interrupt is not None:
        interrupted = ext_interrupt
    else:
        interrupted = threading.Event()

        def _on_sigint(_signum, _frame):
            interrupted.set()

        original_handler = signal.signal(signal.SIGINT, _on_sigint)

    # Semaphore to prevent unbounded internal queue — the submit loop blocks
    # when *concurrency* workers are busy, matching the "同时执行不超过C" spirit.
    sem = threading.BoundedSemaphore(concurrency)
    session = requests.Session()

    def _worker(sess: requests.Session, url: str, timeout_sec: float, stats: Stats):
        try:
            do_request(sess, url, timeout_sec, stats)
        finally:
            sem.release()

    scheduled = 0
    start_time = time.monotonic()

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = []

            # --- Submit phase ---
            for _ in range(num_requests):
                if interrupted.is_set():
                    break
                sem.acquire()   # blocks until a worker slot is free
                future = executor.submit(_worker, session, url, timeout_sec, stats)
                futures.append(future)
                scheduled += 1

            # --- Drain phase ---
            # Wait for all submitted futures; on interrupt, cancel still-queued ones
            for future in as_completed(futures):
                if interrupted.is_set():
                    for f in futures:
                        f.cancel()
                try:
                    future.result(timeout=timeout_sec * 2)
                except Exception:
                    pass  # errors already recorded in Stats

            # --- Release semaphore for cancelled futures ---
            # Futures that were cancelled (never started) did not run _worker,
            # so their sem.release() was never called.  Release those slots now
            # to keep the semaphore count accurate (avoids "leaked" acquires).
            cancelled = sum(1 for f in futures if f.cancelled())
            for _ in range(cancelled):
                sem.release()

    finally:
        session.close()
        if original_handler is not None:
            signal.signal(signal.SIGINT, original_handler)

    elapsed = time.monotonic() - start_time
    return stats, elapsed, interrupted.is_set(), scheduled, concurrency


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    timeout_sec = validate_args(args)

    print(f"Starting {args.requests} requests to {args.url} "
          f"(concurrency={args.concurrency}, timeout={args.timeout})")
    print("Press Ctrl+C to stop early.\n")
    sys.stdout.flush()

    stats, elapsed, was_interrupted, scheduled, adjusted_concurrency = run_load_test(
        url=args.url,
        num_requests=args.requests,
        concurrency=args.concurrency,
        timeout_sec=timeout_sec,
    )

    if adjusted_concurrency != args.concurrency:
        print(f"Note: concurrency adjusted from {args.concurrency} "
              f"to {adjusted_concurrency} (concurrency > requests, using requests count)")

    print_results(
        url=args.url,
        requested=args.requests,
        concurrency=adjusted_concurrency,
        stats=stats,
        elapsed=elapsed,
        interrupted=was_interrupted,
        scheduled=scheduled,
    )

    if was_interrupted:
        print("\n[WARN] Load test was interrupted by user.")
        sys.exit(130)
    else:
        print("\n[DONE] Load test completed.")


if __name__ == "__main__":
    main()
