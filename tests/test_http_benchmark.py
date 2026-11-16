import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import time

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import http_loadtest
import verify_live_http
from http_loadtest import run_http, verdict_outcome
from security import verify_network

KEY = bytes.fromhex("a2" * 32)


def test_http_benchmark_reports_client_shedding_and_actual_signed_requests():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["content-length"]))
            assert verify_network(body, self.headers["x-timestamp"], self.headers["x-signature"], KEY) is None
            data = json.loads(body)
            assert data["event_type"] == "CALL" and "duration_s" not in data
            time.sleep(.035)
            content = json.dumps({"decision": "ALLOW", "ingest": "ADMITTED", "mode": "shadow",
                                  "scored": True, "scored_before_deadline": True, "latency_ms": 1.}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        report = asyncio.run(run_http(f"http://127.0.0.1:{server.server_port}", KEY,
                                      rate=200, seconds=.2, concurrency=2))
        assert report["client_shed"] > 0
        assert report["completed_requests"] + report["client_shed"] == report["offered_events"]
        assert sum(report["http_status"].values()) == report["completed_requests"]
        assert sum(report["response_outcomes"].values()) == report["offered_events"]
        assert report["responses_within_30ms_pct"] < 100
        assert report["http_status"].get("200", 0) > 0
        assert report["unexpected_enforce"] == 0
        assert report["scored_before_deadline"] == report["http_status"]["200"]
        assert report["scored_before_deadline_pct"] == round(
            100 * report["scored_before_deadline"] / report["offered_events"], 3)
        assert report["scored_before_deadline_pct"] < 100
        assert report["scored_within_http_30ms"] == 0
        assert report["denominator"] == "offered_events"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def mock_client(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(http_loadtest.httpx, "AsyncClient",
                        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))


def test_http_benchmark_separates_scoring_pending_failopen_and_errors(monkeypatch):
    responses = [
        {"scored": True, "scored_before_deadline": True, "latency_ms": 1., "commit_ms": .2, "queue_ms": .1},
        {"scored": False, "pending": True, "fail_open": True, "latency_ms": 1.},
        {"scored": False, "pending": False, "fail_open": True, "latency_ms": 1.},
        {"ingest": "ADMITTED", "latency_ms": 1.},  # Legacy admission is not scoring evidence.
        {"scored": True, "scored_before_deadline": False, "latency_ms": 1.},
        {"scored": True, "scored_before_deadline": True, "latency_ms": 35.},
        {"scored": False, "scored_before_deadline": True, "latency_ms": 1.},
        {"scored": True},  # Missing server timing cannot establish the deadline.
        "http_error", "invalid_json", "client_error", {"scored": False, "latency_ms": 1.},
    ]

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"ok": True})
        assert verify_network(request.content, request.headers["x-timestamp"],
                              request.headers["x-signature"], KEY) is None
        index = int(json.loads(request.content)["event_id"].rsplit("-", 1)[1])
        result = responses[index]
        if result == "client_error":
            raise httpx.ConnectError("deliberate connection failure", request=request)
        if result == "http_error":
            return httpx.Response(503, json={"detail": "unavailable"})
        if result == "invalid_json":
            return httpx.Response(200, text="{broken")
        return httpx.Response(200, json={"decision": "ALLOW", "mode": "shadow", **result})

    mock_client(monkeypatch, handler)
    report = asyncio.run(run_http("http://isolated.test", KEY, rate=1000, seconds=.012, concurrency=32))
    assert report["offered_events"] == report["completed_requests"] == 12
    assert report["client_shed"] == 0
    assert report["scored_before_deadline"] == 1
    assert report["scored_before_deadline_pct"] == round(100 / 12, 3)
    assert report["pending_responses"] == 1
    assert report["fail_open"] == 2
    assert report["scoring_unreported"] == 1
    expected = {"scored_before_deadline": 1, "pending": 1, "fail_open": 1, "scored_after_deadline": 2,
                "scored_deadline_unverified": 1, "not_scored": 1, "scoring_unreported": 1,
                "scoring_inconsistent": 1, "http_error": 1, "invalid_response": 1, "client_error": 1,
                "client_shed": 0}
    assert report["response_outcomes"] == expected
    assert sum(report["response_outcomes"].values()) == report["offered_events"]
    assert report["http_status"] == {"200": 10, "503": 1, "client_error": 1}
    assert report["scored_within_http_30ms"] <= report["scored_before_deadline"]
    assert report["server_commit_ms"]["p50"] == .2
    assert report["server_queue_ms"]["p50"] == .1
    # Earlier report keys remain available for direct comparisons.
    assert {"kind", "offered_rate_eps", "completed_eps", "decisions", "ingest", "fail_open",
            "latency_ms", "scheduler_delay_ms", "request_roundtrip_ms", "server_reported_latency_ms",
            "responses_within_30ms_pct", "note"}.issubset(report)


def test_server_scored_deadline_does_not_imply_http_deadline(monkeypatch):
    async def handler(request):
        if request.method == "GET":
            return httpx.Response(200)
        await asyncio.sleep(.04)
        return httpx.Response(200, json={"scored": True, "scored_before_deadline": True,
                                         "latency_ms": 1., "decision": "ALLOW"})

    mock_client(monkeypatch, handler)
    report = asyncio.run(run_http("http://isolated.test", KEY, rate=10, seconds=.1))
    assert report["scored_before_deadline"] == 1
    assert report["scored_before_deadline_pct"] == 100
    assert report["server_responses_within_30ms"] == 1
    assert report["http_responses_within_30ms"] == report["scored_within_http_30ms"] == 0


@pytest.mark.parametrize(("result", "expected"), [
    ({"scored": True, "latency_ms": 30.}, "scored_before_deadline"),
    ({"scored": True, "latency_ms": 30.001}, "scored_after_deadline"),
    ({"scored": True, "scored_before_deadline": False, "latency_ms": 1.}, "scored_after_deadline"),
    ({"scored": False, "scored_before_deadline": True, "latency_ms": 1.}, "scoring_inconsistent"),
    ({"scored": True, "pending": True, "latency_ms": 1.}, "pending"),
    ({"scored": True, "fail_open": True, "latency_ms": 1.}, "fail_open"),
    ({"ingest": "ADMITTED", "latency_ms": 1.}, "scoring_unreported"),
    ({"scored": True, "latency_ms": float("nan")}, "scored_deadline_unverified"),
    ({"scored": True, "latency_ms": float("inf")}, "scored_deadline_unverified"),
    ({"scored": True, "latency_ms": -1.}, "scored_deadline_unverified"),
    ({"scored": True, "latency_ms": True}, "scored_deadline_unverified"),
])
def test_scoring_deadline_requires_valid_evidence(result, expected):
    assert verdict_outcome(result) == expected


@pytest.mark.parametrize("changes", [
    {"rate": float("nan")}, {"rate": float("inf")}, {"rate": 0}, {"rate": -1},
    {"seconds": float("nan")}, {"seconds": float("inf")}, {"seconds": 0}, {"seconds": -1},
    {"concurrency": 0}, {"concurrency": float("inf")}, {"concurrency": 1.5},
    {"timeout_s": float("nan")}, {"timeout_s": 0},
    {"rate": 1.e308, "seconds": 1.e308}, {"rate": .1, "seconds": .1},
])
def test_invalid_run_parameters_never_initialize_http(monkeypatch, changes):
    def unexpected_client(**kwargs):
        raise AssertionError("invalid runs must fail before client initialization")
    monkeypatch.setattr(http_loadtest.httpx, "AsyncClient", unexpected_client)
    options = dict(rate=10., seconds=1., concurrency=2, timeout_s=1.)
    with pytest.raises(ValueError):
        asyncio.run(run_http("http://unused", KEY, **{**options, **changes}))


def test_verify_cli_refuses_existing_output_before_any_benchmark(tmp_path, monkeypatch, capsys):
    destination = tmp_path / "existing.json"
    destination.write_text("preserve this report", encoding="utf-8")

    def unexpected_run(**kwargs):
        raise AssertionError("existing output must be rejected before any benchmark")
    monkeypatch.setattr(verify_live_http, "verify_stack", unexpected_run)
    with pytest.raises(SystemExit) as error:
        verify_live_http.main(["--out", str(destination)])
    assert error.value.code == 2
    assert "output already exists" in capsys.readouterr().err
    assert destination.read_text(encoding="utf-8") == "preserve this report"

    monkeypatch.setattr(http_loadtest, "run_http", unexpected_run)
    with pytest.raises(SystemExit) as error:
        http_loadtest.main(["--url", "http://unused", "--key-file", str(tmp_path / "missing.key"),
                            "--out", str(destination)])
    assert error.value.code == 2
    assert "output already exists" in capsys.readouterr().err


@pytest.mark.parametrize("arguments", [
    ["--rates", "nan"], ["--rates", "inf"], ["--rates", "0"], ["--rates=-1"],
    ["--rates", "100,,500"], ["--rates", ""], ["--seconds", "nan"], ["--seconds", "inf"],
    ["--seconds", "0"], ["--seconds=-1"], ["--concurrency", "0"],
])
def test_verify_cli_rejects_invalid_parameters_before_start(monkeypatch, arguments):
    def unexpected_run(**kwargs):
        raise AssertionError("invalid CLI arguments must not start a server")
    monkeypatch.setattr(verify_live_http, "verify_stack", unexpected_run)
    with pytest.raises(SystemExit) as error:
        verify_live_http.main(arguments)
    assert error.value.code == 2


def test_verify_cli_forwards_concurrency_and_finite_rates(monkeypatch, capsys):
    received = {}
    def capture(**kwargs):
        received.update(kwargs)
        return {"runs": []}
    monkeypatch.setattr(verify_live_http, "verify_stack", capture)
    verify_live_http.main(["--rates", "1,2", "--seconds", "1", "--concurrency", "7"])
    assert received == {"rates": (1., 2.), "seconds": 1., "concurrency": 7, "model_dir": None}
    assert json.loads(capsys.readouterr().out) == {"runs": []}


def test_verify_stack_validates_before_creating_credentials_or_server(monkeypatch):
    def unexpected_directory(**kwargs):
        raise AssertionError("invalid parameters must not create temporary credentials/state")
    monkeypatch.setattr(verify_live_http.tempfile, "mkdtemp", unexpected_directory)
    for options in ({"rates": ()}, {"rates": (float("nan"),)}, {"seconds": float("inf")},
                    {"concurrency": 0}):
        with pytest.raises(ValueError):
            verify_live_http.verify_stack(**options)


@pytest.mark.parametrize("exit_code", [7, -1073741819])
def test_stop_server_reports_child_failure_when_stdin_breaks(exit_code):
    class BrokenStdin:
        def write(self, value):
            pass
        def flush(self):
            raise OSError(22, "Invalid argument")
        def close(self):
            raise OSError(22, "Invalid argument")
    class ExitingProcess:
        stdin = BrokenStdin()
        returncode = None
        def poll(self):
            return None  # exits between this check and flush
        def wait(self, timeout):
            self.returncode = exit_code
    import io
    process, log = ExitingProcess(), io.StringIO()
    with pytest.raises(RuntimeError, match=f"exited with code {exit_code}") as error:
        verify_live_http.stop_server(process, log)
    assert isinstance(error.value.__cause__, OSError)
    assert log.closed


def test_stop_server_handles_already_clean_exit():
    import io
    from types import SimpleNamespace
    pipe, log = io.StringIO(), io.StringIO()
    process = SimpleNamespace(stdin=pipe, returncode=0, poll=lambda: 0)
    verify_live_http.stop_server(process, log)
    assert pipe.closed and log.closed
