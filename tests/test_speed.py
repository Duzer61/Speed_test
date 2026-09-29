"""Тесты замерятеля скорости на локальном HTTP-сервере (без выхода в интернет).

Запуск::

    uv run python -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import http.server
import io
import sys
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import main as speedtest  # noqa: E402

PAYLOAD = b"speed-test-payload-" * 256  # 4608 байт
CLOSED_PORT_URL = "http://127.0.0.1:1/payload.bin"


class _PayloadServer(http.server.ThreadingHTTPServer):
    """HTTP-сервер, отдающий заранее заданное тело ответа."""

    daemon_threads = True
    allow_reuse_address = True

    payload: bytes = b""
    status_code: int = 200
    paths: list[str]


class _PayloadHandler(http.server.BaseHTTPRequestHandler):
    """Обработчик, который отдаёт ``_PayloadServer.payload``."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 (имя задано базовым классом)
        server = self.server
        assert isinstance(server, _PayloadServer)
        server.paths.append(self.path)
        body = server.payload if server.status_code < 400 else b""
        self.send_response(server.status_code)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Отключает вывод логов сервера в консоль."""


class LocalHttpServer:
    """Контекстный менеджер: HTTP-сервер на свободном порту ``127.0.0.1``."""

    def __init__(self, payload: bytes = b"", status: int = 200) -> None:
        self.url = ""
        self.paths: list[str] = []
        self._payload = payload
        self._status = status
        self._server: _PayloadServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> LocalHttpServer:
        server = _PayloadServer(("127.0.0.1", 0), _PayloadHandler)
        server.payload = self._payload
        server.status_code = self._status
        server.paths = self.paths
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        # host известен заранее (биндим 127.0.0.1), из адреса берём только порт
        port = int(server.server_address[1])
        self.url = f"http://127.0.0.1:{port}/payload.bin"
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


class NormalizeUrlTests(unittest.TestCase):
    """Проверки нормализации введённого адреса."""

    def test_adds_https_scheme(self) -> None:
        self.assertEqual(
            speedtest.normalize_url("example.com/big.jpg"),
            "https://example.com/big.jpg",
        )

    def test_keeps_existing_scheme(self) -> None:
        url = "http://example.com/big.jpg"
        self.assertEqual(speedtest.normalize_url(url), url)

    def test_strips_spaces_and_quotes(self) -> None:
        self.assertEqual(
            speedtest.normalize_url('  "https://example.com/a.bin"  '),
            "https://example.com/a.bin",
        )

    def test_empty_url_raises(self) -> None:
        with self.assertRaises(ValueError):
            speedtest.normalize_url("   ")

    def test_unsupported_scheme_raises(self) -> None:
        with self.assertRaises(ValueError):
            speedtest.normalize_url("ftp://example.com/a.bin")

    def test_missing_host_raises(self) -> None:
        with self.assertRaises(ValueError):
            speedtest.normalize_url("https://")


class CacheBusterTests(unittest.TestCase):
    """Проверки параметра обхода кэша."""

    def test_adds_unique_parameter(self) -> None:
        first = speedtest.with_cache_buster("https://example.com/a.bin", nonce=1)
        second = speedtest.with_cache_buster("https://example.com/a.bin", nonce=2)
        self.assertEqual(first, "https://example.com/a.bin?_cb=1")
        self.assertEqual(second, "https://example.com/a.bin?_cb=2")

    def test_keeps_existing_query(self) -> None:
        result = speedtest.with_cache_buster("https://example.com/a.bin?x=1", nonce=7)
        self.assertIn("x=1", result)
        self.assertIn("_cb=7", result)


class ReportMathTests(unittest.TestCase):
    """Проверки арифметики отчёта без обращения к сети."""

    def setUp(self) -> None:
        self.report = speedtest.Report(
            url="https://example.com/a.bin",
            requested_count=3,
            results=[
                speedtest.RequestResult(
                    index=1, url="u", bytes_read=speedtest.BYTES_IN_MB, elapsed=1.0, ttfb=0.1
                ),
                speedtest.RequestResult(
                    index=2, url="u", bytes_read=speedtest.BYTES_IN_MB, elapsed=2.0, ttfb=0.2
                ),
                speedtest.RequestResult(index=3, url="u", error="HTTP 500"),
            ],
        )

    def test_successful_results_filtering(self) -> None:
        self.assertEqual(len(self.report.ok_results), 2)
        self.assertEqual(len(self.report.failed_results), 1)
        self.assertFalse(self.report.failed_results[0].ok)

    def test_totals(self) -> None:
        self.assertEqual(self.report.total_bytes, 2 * speedtest.BYTES_IN_MB)
        self.assertAlmostEqual(self.report.total_time, 3.0, places=9)

    def test_time_statistics(self) -> None:
        self.assertAlmostEqual(self.report.avg_time, 1.5, places=9)
        self.assertAlmostEqual(self.report.min_time, 1.0, places=9)
        self.assertAlmostEqual(self.report.max_time, 2.0, places=9)
        self.assertAlmostEqual(self.report.median_time, 1.5, places=9)

    def test_speed_calculation(self) -> None:
        self.assertAlmostEqual(self.report.avg_mb_per_s, 2 / 3, places=9)
        self.assertAlmostEqual(
            self.report.avg_mbit_per_s, self.report.avg_mb_per_s * 8.388608, places=6
        )

    def test_empty_report_gives_zero(self) -> None:
        empty = speedtest.Report(url="u", requested_count=0, results=[])
        self.assertEqual(empty.avg_time, 0.0)
        self.assertEqual(empty.avg_mb_per_s, 0.0)
        self.assertEqual(empty.median_time, 0.0)


class MeasureTests(unittest.TestCase):
    """Проверки замеров на локальном сервере."""

    def test_downloads_payload_for_each_request(self) -> None:
        with LocalHttpServer(PAYLOAD) as server:
            report = speedtest.measure(server.url, count=3, timeout=5.0, verbose=False)
        self.assertEqual(report.requested_count, 3)
        self.assertEqual(len(report.results), 3)
        self.assertEqual(len(report.ok_results), 3)
        self.assertEqual(report.total_bytes, 3 * len(PAYLOAD))
        self.assertAlmostEqual(report.avg_time, report.total_time / 3, places=9)
        self.assertGreater(report.avg_time, 0.0)
        self.assertGreater(report.avg_mb_per_s, 0.0)
        self.assertAlmostEqual(
            report.avg_mbit_per_s, report.avg_mb_per_s * speedtest.BYTES_IN_MB * 8 / 10**6,
            places=9,
        )
        self.assertEqual(len(server.paths), 3)

    def test_every_request_has_unique_cache_buster(self) -> None:
        with LocalHttpServer(PAYLOAD) as server:
            speedtest.measure(server.url, count=4, timeout=5.0, verbose=False)
        self.assertEqual(len(server.paths), 4)
        self.assertTrue(all("_cb=" in path for path in server.paths))
        self.assertEqual(len(set(server.paths)), 4)

    def test_cache_bust_disabled_uses_same_url(self) -> None:
        with LocalHttpServer(PAYLOAD) as server:
            speedtest.measure(
                server.url, count=3, timeout=5.0, cache_bust=False, verbose=False
            )
        self.assertEqual(len(set(server.paths)), 1)
        self.assertNotIn("_cb=", server.paths[0])

    def test_http_error_is_reported(self) -> None:
        with LocalHttpServer(PAYLOAD, status=404) as server:
            report = speedtest.measure(server.url, count=2, timeout=5.0, verbose=False)
        self.assertEqual(len(report.results), 2)
        self.assertEqual(report.ok_results, [])
        self.assertEqual(report.total_bytes, 0)
        self.assertEqual(report.avg_mb_per_s, 0.0)
        for result in report.results:
            self.assertFalse(result.ok)
            self.assertIn("404", result.error or "")

    def test_connection_error_is_reported(self) -> None:
        report = speedtest.measure(CLOSED_PORT_URL, count=1, timeout=1.0, verbose=False)
        self.assertEqual(len(report.results), 1)
        self.assertFalse(report.results[0].ok)
        self.assertEqual(report.total_bytes, 0)


class ErrorMessageTests(unittest.TestCase):
    """Проверки человекочитаемых сообщений об ошибках."""

    def test_connection_error_is_collapsed(self) -> None:
        exc = speedtest.requests.ConnectionError(
            'HTTPConnectionPool(host="h", port=80): Max retries exceeded with url: /x '
            '(Caused by NewConnectionError("boom"))'
        )
        message = speedtest.describe_error(exc)
        self.assertEqual(message.count("("), message.count(")"))
        self.assertIn("NewConnectionError", message)
        self.assertNotIn("Max retries exceeded", message)

    def test_http_error_message(self) -> None:
        response = speedtest.requests.Response()
        response.status_code = 503
        response.reason = "Service Unavailable"
        exc = speedtest.requests.HTTPError("503 Server Error", response=response)
        self.assertEqual(speedtest.describe_error(exc), "HTTP 503 Service Unavailable")
        self.assertEqual(speedtest.status_from_error(exc), 503)

    def test_timeout_message(self) -> None:
        self.assertEqual(speedtest.describe_error(speedtest.requests.Timeout()), "таймаут запроса")


class MainTests(unittest.TestCase):
    """Проверки CLI-обвязки."""

    def test_returns_zero_on_success(self) -> None:
        with LocalHttpServer(PAYLOAD) as server, contextlib.redirect_stdout(io.StringIO()):
            code = speedtest.main([server.url, "-n", "2", "-q"])
        self.assertEqual(code, 0)

    def test_invalid_url_returns_two(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = speedtest.main(["ftp://example.com/a.bin"])
        self.assertEqual(code, 2)
        self.assertIn("Некорректный адрес", stderr.getvalue())

    def test_all_requests_failed_returns_one(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = speedtest.main([CLOSED_PORT_URL, "-n", "1", "-t", "1", "-q"])
        self.assertEqual(code, 1)
        self.assertIn("Ни один запрос не завершился успешно", stderr.getvalue())
        self.assertIn("Нет успешных запросов", stdout.getvalue())

    def test_count_must_be_positive(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
            speedtest.parse_args(["https://example.com/a.bin", "-n", "0"])
        self.assertEqual(ctx.exception.code, 2)

    def test_default_count_is_ten(self) -> None:
        args = speedtest.parse_args(["https://example.com/a.bin"])
        self.assertEqual(args.count, speedtest.DEFAULT_COUNT)
        self.assertEqual(args.count, 10)
        self.assertEqual(args.timeout, speedtest.DEFAULT_TIMEOUT)
        self.assertFalse(args.no_cache_bust)


if __name__ == "__main__":
    unittest.main()
