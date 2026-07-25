#!/usr/bin/env python3
"""
Automated tests for httpload — zero dependency on public network.

Each test starts its own HTTP server on localhost with a random port,
runs the load-test engine, then asserts on the collected stats.
"""

import unittest
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

import httpload


# ---------------------------------------------------------------------------
# Test HTTP server helpers
# ---------------------------------------------------------------------------

class _TestHandler(BaseHTTPRequestHandler):
    """Handler whose behaviour is controlled by class-level callbacks."""
    handler_fn = None

    def do_GET(self):
        code, body = _TestHandler.handler_fn(self.path)
        self.send_response(code)
        self.end_headers()
        self.wfile.write(body.encode() if isinstance(body, str) else body)

    def log_message(self, fmt, *args):
        pass


def _start_server(handler_fn):
    """Start an HTTP server on localhost:0, return (server, port)."""
    _TestHandler.handler_fn = handler_fn
    server = HTTPServer(("127.0.0.1", 0), _TestHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def _free_port():
    """Return a port number that is likely free on localhost."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# -- handler factories ---------------------------------------------------------

def _ok_handler(_path):
    return (200, "OK")


def _not_found_handler(_path):
    return (404, "not found")


def _mixed_handler(_path):
    """Every other request is 404; the rest are 200."""
    if not hasattr(_mixed_handler, "counter"):
        _mixed_handler.counter = 0
    _mixed_handler.counter += 1
    if _mixed_handler.counter % 2 == 0:
        return (404, "not found")
    return (200, "OK")


def _slow_handler(_path):
    time.sleep(3)   # longer than any test timeout
    return (200, "too late")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBasicSuccess(unittest.TestCase):
    """Verify that 100 % 2xx responses produce correct stats."""

    def test_all_success(self):
        server, port = _start_server(_ok_handler)
        try:
            stats, elapsed, interrupted, scheduled, concurrency = httpload.run_load_test(
                url=f"http://127.0.0.1:{port}/",
                num_requests=100,
                concurrency=10,
                timeout_sec=2.0,
            )
            self.assertFalse(interrupted)
            self.assertEqual(scheduled, 100)
            self.assertEqual(stats.completed, 100)
            self.assertEqual(stats.succeeded, 100)
            self.assertEqual(stats.non2xx, 0)
            self.assertEqual(stats.errors, 0)
            self.assertEqual(stats.timeouts, 0)

            # Conservation: _completed is independent — confirms no
            # double-counting or skipped categories.
            self.assertTrue(stats.conservation_ok())
            self.assertEqual(
                stats.completed,
                stats.succeeded + stats.non2xx + stats.errors + stats.timeouts,
            )

            # In-flight peak ≤ concurrency
            self.assertGreater(stats.max_in_flight, 0)
            self.assertLessEqual(stats.max_in_flight, concurrency)

            # Latency records
            self.assertEqual(len(stats.latencies), 100)
            for lat in stats.latencies:
                self.assertGreaterEqual(lat, 0)
            avg = sum(stats.latencies) / len(stats.latencies)
            self.assertLess(avg, 1.0)
        finally:
            server.shutdown()


class TestNon2xx(unittest.TestCase):
    """Verify correct classification of non-2xx status codes."""

    def test_all_404(self):
        server, port = _start_server(_not_found_handler)
        try:
            stats, _, _, _, _ = httpload.run_load_test(
                url=f"http://127.0.0.1:{port}/any",
                num_requests=50,
                concurrency=10,
                timeout_sec=2.0,
            )
            self.assertEqual(stats.completed, 50)
            self.assertEqual(stats.succeeded, 0)
            self.assertEqual(stats.non2xx, 50)
            self.assertEqual(stats.errors, 0)
            self.assertEqual(stats.timeouts, 0)
            self.assertTrue(stats.conservation_ok())
            self.assertLessEqual(stats.max_in_flight, 10)
        finally:
            server.shutdown()

    def test_mixed_200_404(self):
        server, port = _start_server(_mixed_handler)
        try:
            stats, _, _, _, _ = httpload.run_load_test(
                url=f"http://127.0.0.1:{port}/",
                num_requests=100,
                concurrency=10,
                timeout_sec=2.0,
            )
            self.assertEqual(stats.completed, 100)
            # With the alternating handler, expect roughly 50/50 split.
            # Allow ±10 tolerance for threading non-determinism.
            self.assertAlmostEqual(stats.succeeded, 50, delta=10)
            self.assertAlmostEqual(stats.non2xx, 50, delta=10)
            self.assertEqual(stats.succeeded + stats.non2xx, 100)
            self.assertTrue(stats.conservation_ok())
            self.assertLessEqual(stats.max_in_flight, 10)
        finally:
            server.shutdown()


class TestTimeout(unittest.TestCase):
    """Verify that slow requests trigger timeout counting."""

    def test_timeout(self):
        server, port = _start_server(_slow_handler)
        try:
            stats, _, _, _, _ = httpload.run_load_test(
                url=f"http://127.0.0.1:{port}/slow",
                num_requests=20,
                concurrency=10,
                timeout_sec=0.5,   # 500ms, while server sleeps 3s
            )
            self.assertEqual(stats.completed, 20)
            self.assertEqual(stats.succeeded, 0)
            # ALL requests should time out (server sleeps 3s > 0.5s timeout).
            # Allow a small tolerance for edge cases.
            self.assertGreaterEqual(stats.timeouts, 18)
            self.assertEqual(stats.errors + stats.timeouts, 20)
            self.assertTrue(stats.conservation_ok())
            self.assertLessEqual(stats.max_in_flight, 10)
        finally:
            server.shutdown()


class TestConcurrencyAdjust(unittest.TestCase):
    """Concurrency > requests should auto-adjust downward."""

    def test_auto_adjust(self):
        server, port = _start_server(_ok_handler)
        try:
            _, _, _, scheduled, concurrency = httpload.run_load_test(
                url=f"http://127.0.0.1:{port}/",
                num_requests=10,
                concurrency=50,   # will be adjusted to 10
                timeout_sec=2.0,
            )
            self.assertEqual(scheduled, 10)
            self.assertEqual(concurrency, 10)
        finally:
            server.shutdown()


class TestStats(unittest.TestCase):
    """Unit-level tests for the Stats class — conservation is NOT tautological."""

    def test_conservation_empty(self):
        s = httpload.Stats()
        self.assertEqual(s.completed, 0)
        self.assertEqual(s.max_in_flight, 0)
        self.assertTrue(s.conservation_ok())

    def test_conservation_after_records(self):
        s = httpload.Stats()
        s.enter_request(); s.exit_request("succeeded", 0.01)
        s.enter_request(); s.exit_request("non2xx",    0.02)
        s.enter_request(); s.exit_request("errors",    0.03)
        s.enter_request(); s.exit_request("timeouts",  0.50)
        self.assertEqual(s.completed, 4)
        self.assertEqual(s.succeeded, 1)
        self.assertEqual(s.non2xx,    1)
        self.assertEqual(s.errors,    1)
        self.assertEqual(s.timeouts,  1)
        self.assertTrue(s.conservation_ok())
        self.assertEqual(len(s.latencies), 4)
        self.assertEqual(s.max_in_flight, 1)  # sequential — never >1

    def test_in_flight_peak_tracking(self):
        """max_in_flight reflects concurrent calls, not total count."""
        s = httpload.Stats()
        # Simulate overlapping requests
        s.enter_request()   # in_flight = 1, peak = 1
        s.enter_request()   # in_flight = 2, peak = 2
        s.enter_request()   # in_flight = 3, peak = 3
        s.exit_request("succeeded", 0.01)  # in_flight = 2
        s.exit_request("succeeded", 0.01)  # in_flight = 1
        s.enter_request()   # in_flight = 2, peak still 3
        s.exit_request("succeeded", 0.01)  # in_flight = 1
        s.exit_request("succeeded", 0.01)  # in_flight = 0

        self.assertEqual(s.completed, 4)
        self.assertEqual(s.max_in_flight, 3)
        self.assertTrue(s.conservation_ok())


class TestInterrupt(unittest.TestCase):
    """Verify graceful shutdown when interrupted mid-flight.

    NOTE: Uses ``ext_interrupt`` Event rather than a real OS signal.
    Real SIGINT cannot be tested portably in an in-process unit test.
    The test exercises the *same* interrupt logic path used by the
    SIGINT handler — the only difference is *how* the Event is set.
    """

    def test_interrupt_stops_cleanly(self):
        server, port = _start_server(_slow_handler)
        try:
            interrupt = threading.Event()
            result = []

            def _run():
                result.append(httpload.run_load_test(
                    url=f"http://127.0.0.1:{port}/slow",
                    num_requests=100,
                    concurrency=10,
                    timeout_sec=0.3,
                    ext_interrupt=interrupt,
                ))

            t = threading.Thread(target=_run)
            t.start()

            time.sleep(0.8)
            interrupt.set()

            t.join(timeout=5)
            self.assertFalse(t.is_alive(), "Load test should not hang after interrupt")

            self.assertEqual(len(result), 1)
            stats, elapsed, was_interrupted, scheduled, concurrency = result[0]

            self.assertTrue(was_interrupted)
            self.assertLess(scheduled, 100)           # stopped scheduling early
            self.assertGreater(stats.completed, 0)    # some requests did complete
            self.assertTrue(stats.conservation_ok())
            self.assertLessEqual(stats.max_in_flight, concurrency)
        finally:
            server.shutdown()


class TestNetworkError(unittest.TestCase):
    """Verify that network-level errors are counted and do NOT deadlock."""

    def test_error_counting_no_deadlock(self):
        """Requests to a dead port: all should be errors, no hang."""
        dead_port = _free_port()   # avoid hard-coded port 19999
        stats, elapsed, interrupted, scheduled, concurrency = httpload.run_load_test(
            url=f"http://127.0.0.1:{dead_port}/",
            num_requests=20,
            concurrency=5,
            timeout_sec=1.0,
        )
        self.assertFalse(interrupted)
        self.assertEqual(scheduled, 20)
        self.assertEqual(stats.completed, 20)
        self.assertEqual(stats.succeeded, 0)
        self.assertEqual(stats.non2xx, 0)
        # Connection refused → errors (not timeouts on most platforms).
        # On Windows firewall-dropped ports may show as timeouts; accept both.
        self.assertEqual(stats.errors + stats.timeouts, 20)
        self.assertTrue(stats.conservation_ok())
        self.assertLessEqual(stats.max_in_flight, concurrency)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
