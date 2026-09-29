"""Скрипт-замерятель скорости интернета.

Принимает адрес (URL тяжёлой картинки или файла), последовательно выполняет
N (по умолчанию 10) запросов, дожидается полного ответа от сервера и печатает
в консоль статистику: среднее время запроса, объём скачанных данных и
среднюю скорость скачивания.

Примеры запуска::

    uv run main.py http://spd-rudp.hostkey.ru/files/10mb.bin
    uv run main.py speedtest.selectel.ru/10MB -n 5
    uv run main.py                      # адрес будет запрошен в консоли
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from dataclasses import dataclass
from typing import Final, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

DEFAULT_COUNT: Final[int] = 10
DEFAULT_TIMEOUT: Final[float] = 30.0
CHUNK_SIZE: Final[int] = 64 * 1024
BYTES_IN_MB: Final[int] = 1024**2
BITS_IN_MBIT: Final[int] = 10**6
USER_AGENT: Final[str] = "speed-test/0.1"


@dataclass(frozen=True)
class RequestResult:
    """Результат одного запроса."""

    index: int
    url: str
    bytes_read: int = 0
    elapsed: float = 0.0
    ttfb: float = 0.0
    status: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        """Успешно ли завершился запрос."""
        return self.error is None

    @property
    def mb_per_s(self) -> float:
        """Скорость скачивания этого запроса, МБ/с."""
        if self.elapsed <= 0 or self.bytes_read <= 0:
            return 0.0
        return self.bytes_read / BYTES_IN_MB / self.elapsed


@dataclass(frozen=True)
class Report:
    """Итоговая статистика замера."""

    url: str
    requested_count: int
    results: list[RequestResult]
    cache_bust: bool = True
    interrupted: bool = False

    @property
    def ok_results(self) -> list[RequestResult]:
        """Список успешных запросов."""
        return [item for item in self.results if item.ok]

    @property
    def failed_results(self) -> list[RequestResult]:
        """Список неудавшихся запросов."""
        return [item for item in self.results if not item.ok]

    @property
    def total_bytes(self) -> int:
        """Суммарно скачанный объём, байт."""
        return sum(item.bytes_read for item in self.ok_results)

    @property
    def total_time(self) -> float:
        """Суммарное время успешных запросов, с."""
        return sum(item.elapsed for item in self.ok_results)

    @property
    def avg_time(self) -> float:
        """Среднее время одного запроса, с."""
        results = self.ok_results
        return self.total_time / len(results) if results else 0.0

    @property
    def min_time(self) -> float:
        """Минимальное время запроса, с."""
        results = self.ok_results
        return min(item.elapsed for item in results) if results else 0.0

    @property
    def max_time(self) -> float:
        """Максимальное время запроса, с."""
        results = self.ok_results
        return max(item.elapsed for item in results) if results else 0.0

    @property
    def median_time(self) -> float:
        """Медиана времени запроса, с."""
        results = self.ok_results
        return statistics.median(item.elapsed for item in results) if results else 0.0

    @property
    def avg_mb_per_s(self) -> float:
        """Средняя скорость скачивания (весь объём / всё время), МБ/с."""
        if self.total_time <= 0:
            return 0.0
        return self.total_bytes / BYTES_IN_MB / self.total_time

    @property
    def avg_mbit_per_s(self) -> float:
        """Средняя скорость скачивания, Мбит/с."""
        return self.avg_mb_per_s * BYTES_IN_MB * 8 / BITS_IN_MBIT

    @property
    def per_request_mb_per_s(self) -> list[float]:
        """Скорости отдельных запросов, МБ/с."""
        return [item.mb_per_s for item in self.ok_results if item.mb_per_s > 0]


def normalize_url(raw: str) -> str:
    """Приводит введённый пользователем адрес к полноценному URL.

    Если схема не указана, подставляется ``https://``.

    :raises ValueError: если адрес пустой, содержит неподдерживаемую схему
        или не содержит хоста.
    """
    url = raw.strip().strip('"').strip("'").strip()
    if not url:
        raise ValueError("адрес не может быть пустым")
    if "://" not in url:
        url = f"https://{url}"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"поддерживаются только http/https, получено {parts.scheme!r}")
    if not parts.netloc:
        raise ValueError(f"не удалось определить хост в адресе {raw!r}")
    return url


def with_cache_buster(url: str, nonce: int) -> str:
    """Добавляет к URL параметр ``_cb``, чтобы сервер не отдавал ответ из кэша."""
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.append(("_cb", str(nonce)))
    return urlunsplit(parts._replace(query=urlencode(query)))


def describe_error(exc: requests.RequestException) -> str:
    """Преобразует исключение requests в короткое человекочитаемое сообщение."""
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        reason = exc.response.reason or ""
        return f"HTTP {exc.response.status_code} {reason}".strip()
    if isinstance(exc, requests.Timeout):
        return "таймаут запроса"
    if isinstance(exc, requests.ConnectionError):
        message = str(exc)
        marker = "Caused by "
        # убираем многословную обёртку urllib3, оставляя первопричину
        if marker in message:
            message = message.split(marker, 1)[1]
            if message.endswith(")"):
                message = message[:-1]
        return f"ошибка соединения: {message}"
    return str(exc) or exc.__class__.__name__


def status_from_error(exc: requests.RequestException) -> int | None:
    """Достаёт HTTP-код из исключения, если он там есть."""
    response = getattr(exc, "response", None)
    return response.status_code if response is not None else None


def download_once(
    session: requests.Session,
    index: int,
    url: str,
    timeout: float,
) -> RequestResult:
    """Выполняет один GET-запрос и полностью вычитывает тело ответа.

    Тело читается чанками по :data:`CHUNK_SIZE` байт, поэтому считается
    реально полученный объём, а не значение заголовка ``Content-Length``.

    Ошибки сети не пробрасываются наружу, а возвращаются в поле
    :attr:`RequestResult.error`, чтобы один сбойный запрос не ломал весь замер.
    """
    started = time.perf_counter()
    ttfb = 0.0
    bytes_read = 0
    status: int | None = None
    try:
        with session.get(
            url,
            stream=True,
            timeout=(timeout, timeout),
            allow_redirects=True,
        ) as response:
            status = response.status_code
            response.raise_for_status()
            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                if not chunk:
                    continue
                if ttfb == 0.0:
                    ttfb = time.perf_counter() - started
                bytes_read += len(chunk)
    except requests.RequestException as exc:
        return RequestResult(
            index=index,
            url=url,
            bytes_read=bytes_read,
            elapsed=time.perf_counter() - started,
            ttfb=ttfb,
            status=status if status is not None else status_from_error(exc),
            error=describe_error(exc),
        )

    elapsed = time.perf_counter() - started
    if ttfb == 0.0:
        ttfb = elapsed
    return RequestResult(
        index=index,
        url=url,
        bytes_read=bytes_read,
        elapsed=elapsed,
        ttfb=ttfb,
        status=status,
    )


def format_size(num_bytes: int) -> str:
    """Форматирует объём в мегабайтах."""
    return f"{num_bytes / BYTES_IN_MB:.2f} МБ"


def format_result_line(result: RequestResult) -> str:
    """Строка отчёта по одному запросу."""
    if not result.ok:
        return f"ошибка: {result.error}"
    return (
        f"{format_size(result.bytes_read):>12} за {result.elapsed:6.3f} c "
        f"(TTFB {result.ttfb:5.3f} c) -> {result.mb_per_s:6.2f} МБ/с"
    )


def measure(
    url: str,
    count: int = DEFAULT_COUNT,
    timeout: float = DEFAULT_TIMEOUT,
    cache_bust: bool = True,
    verbose: bool = True,
) -> Report:
    """Последовательно выполняет ``count`` запросов и собирает статистику.

    Используется одна :class:`requests.Session`, чтобы соединение
    переиспользовалось (keep-alive) и замер не включал повторное
    TCP/TLS-рукопожатие на каждом запросе.
    """
    session_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
    }
    if cache_bust:
        session_headers.update({"Cache-Control": "no-cache", "Pragma": "no-cache"})

    results: list[RequestResult] = []
    interrupted = False

    with requests.Session() as session:
        session.headers.update(session_headers)
        for index in range(1, count + 1):
            request_url = (
                with_cache_buster(url, nonce=time.time_ns() // 1_000_000)
                if cache_bust
                else url
            )
            if verbose:
                print(f"Запрос {index}/{count} ... ", end="", flush=True)
            try:
                result = download_once(session, index, request_url, timeout)
            except KeyboardInterrupt:
                interrupted = True
                print("\nПрервано пользователем (Ctrl+C).")
                break
            results.append(result)
            if verbose:
                print(format_result_line(result))

    return Report(
        url=url,
        requested_count=count,
        results=results,
        cache_bust=cache_bust,
        interrupted=interrupted,
    )


def print_report(report: Report) -> None:
    """Печатает итоговую статистику замера."""
    ok_count = len(report.ok_results)
    separator = "=" * 72
    print(separator)
    print(f"Адрес:                  {report.url}")
    print(
        f"Запросов:               {len(report.results)} из {report.requested_count} "
        f"(успешных: {ok_count}, с ошибкой: {len(report.failed_results)})"
    )
    print(
        "Обход кэша:             включён (_cb=... и Cache-Control: no-cache)"
        if report.cache_bust
        else "Обход кэша:             выключен (ответы могут браться из кэша)"
    )
    if report.interrupted:
        print("Замер прерван пользователем, статистика по выполненным запросам.")

    for result in report.failed_results:
        print(f"  ! запрос #{result.index} ({result.status}) — {result.error}")

    if ok_count == 0:
        print("Нет успешных запросов — статистику посчитать невозможно.")
        print(separator)
        return

    print("-" * 72)
    print(
        f"Среднее время запроса:  {report.avg_time:.3f} c "
        f"(min {report.min_time:.3f} / max {report.max_time:.3f} / "
        f"медиана {report.median_time:.3f})"
    )
    print(
        f"Скачано данных:         {format_size(report.total_bytes)} "
        f"({report.total_bytes} байт)"
    )
    print(f"Суммарное время:        {report.total_time:.3f} c")
    print(
        f"Средняя скорость:       {report.avg_mb_per_s:.2f} МБ/с "
        f"({report.avg_mbit_per_s:.1f} Мбит/с)"
    )

    rates = report.per_request_mb_per_s
    if len(rates) > 1 and min(rates) != max(rates):
        print(
            f"Разброс по запросам:    {min(rates):.2f} ... {max(rates):.2f} МБ/с, "
            f"среднее по запросам {statistics.fmean(rates):.2f} МБ/с"
        )
    print(separator)


def positive_int(value: str) -> int:
    """argparse-валидатор: целое число больше нуля."""
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} не является целым числом") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("значение должно быть больше нуля")
    return number


def positive_float(value: str) -> float:
    """argparse-валидатор: положительное число."""
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} не является числом") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("значение должно быть больше нуля")
    return number


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Разбирает аргументы командной строки."""
    parser = argparse.ArgumentParser(
        prog="speed-test",
        description="Последовательно запрашивает адрес и считает среднюю скорость скачивания.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=None,
        help="адрес файла/картинки для замера; если не указан — будет запрошен в консоли",
    )
    parser.add_argument(
        "-n", "--count", type=positive_int, default=DEFAULT_COUNT,
        help="число последовательных запросов",
    )
    parser.add_argument(
        "-t", "--timeout", type=positive_float, default=DEFAULT_TIMEOUT,
        help="таймаут одного запроса, с",
    )
    parser.add_argument(
        "--no-cache-bust", action="store_true",
        help="не добавлять параметр обхода кэша _cb=...",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true",
        help="печатать только итоговую статистику",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа. Возвращает код возврата процесса."""
    args = parse_args(argv)

    raw_url: str | None = args.url
    if not raw_url:
        try:
            raw_url = input("Введите адрес (URL) для замера: ")
        except (EOFError, KeyboardInterrupt):
            print("\nАдрес не введён.", file=sys.stderr)
            return 2

    try:
        url = normalize_url(raw_url)
    except ValueError as exc:
        print(f"Некорректный адрес: {exc}.", file=sys.stderr)
        return 2

    print(f"Замер скорости: {url}")
    print(f"Выполняю запросов: {args.count}, таймаут одного запроса: {args.timeout:g} c.")
    report = measure(
        url,
        count=args.count,
        timeout=args.timeout,
        cache_bust=not args.no_cache_bust,
        verbose=not args.quiet,
    )
    print_report(report)

    if not report.ok_results:
        print("Ни один запрос не завершился успешно.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
