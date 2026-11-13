from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
HOST = os.environ.get("HOST", "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
PORT = int(os.environ.get("PORT") or os.environ.get("FEISHU_DASHBOARD_PORT") or "8766")
FEISHU_BASE = "https://open.feishu.cn/open-apis"
CHUNK_ROWS = 600
CHUNK_CONCURRENCY = max(1, int(os.environ.get("FEISHU_CHUNK_CONCURRENCY", "4")))
REQUEST_TIMEOUT = 120
REQUEST_RETRIES = 3
CACHE_DIR = Path(os.environ.get("FEISHU_SYNC_CACHE_DIR", str(ROOT)))
LEGACY_CACHE_FILE = ROOT / ".feishu_sync_cache.json"
SYNC_CONCURRENCY = max(1, int(os.environ.get("FEISHU_SYNC_CONCURRENCY", "2")))
SYNC_SLOTS = threading.BoundedSemaphore(SYNC_CONCURRENCY)

SHEET_MAPPING = [
    ("instantStores", "零售门店"),
    ("instantOrders", "零售订单明细"),
    ("instantProducts", "零售订单商品"),
    ("douyinStores", "抖音门店记录表"),
    ("douyinOrders", "抖音订单"),
    ("douyinVerify", "抖音核销"),
    ("douyinAfterSales", "抖音售后"),
]


class FeishuApiError(Exception):
    def __init__(
        self,
        message: str,
        status: int = 502,
        code: int | str | None = None,
        fallback_allowed: bool = False,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.fallback_allowed = fallback_allowed


def http_json(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: int = REQUEST_TIMEOUT,
) -> dict[str, Any]:
    body = None
    request_headers = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    for attempt in range(REQUEST_RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except Exception as error:
                raise FeishuApiError("飞书接口返回了无法解析的数据", status=502) from error
            if parsed.get("code") not in (0, None):
                raise FeishuApiError(parsed.get("msg") or "飞书接口调用失败", status=502, code=parsed.get("code"))
            return parsed
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                parsed = json.loads(raw.decode("utf-8"))
                message = parsed.get("msg") or f"Feishu HTTP {error.code}"
                code = parsed.get("code")
            except Exception:
                message = f"Feishu HTTP {error.code}"
                code = None
            if error.code in (429, 500, 502, 503, 504) and attempt < REQUEST_RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise FeishuApiError(
                message,
                status=502,
                code=code,
                fallback_allowed=error.code in (429, 500, 502, 503, 504),
            ) from error
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as error:
            if attempt < REQUEST_RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            reason = getattr(error, "reason", str(error))
            reason_text = str(reason)
            if "10013" in reason_text or "access permissions" in reason_text.lower():
                message = "同步服务被 Windows 网络权限限制。请关闭当前服务窗口，双击“启动飞书看板服务.cmd”后再同步"
            else:
                message = f"飞书接口读取超时或网络中断，已自动重试 {REQUEST_RETRIES} 次：{reason_text}"
            raise FeishuApiError(
                message,
                status=502,
                fallback_allowed=True,
            ) from error
    raise FeishuApiError("飞书接口调用失败", status=502)


def get_tenant_token(app_id: str, app_secret: str) -> str:
    response = http_json(
        f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
        method="POST",
        payload={"app_id": app_id, "app_secret": app_secret},
    )
    token = response.get("tenant_access_token")
    if not token:
        raise FeishuApiError("飞书未返回 tenant_access_token")
    return token


def parse_sheet_token(value: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError("请输入 Sheet Token 或飞书表格链接")
    match = re.search(r"/(?:wiki|sheets)/([A-Za-z0-9]+)", text)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9]+", text):
        return text
    raise ValueError("无法从输入中识别 Sheet Token")


def api_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def get_spreadsheet(token: str, sheet_token: str) -> dict[str, Any]:
    response = http_json(
        f"{FEISHU_BASE}/sheets/v3/spreadsheets/{urllib.parse.quote(sheet_token)}",
        headers=api_headers(token),
    )
    return response.get("data", {}).get("spreadsheet", {})


def get_sheets(token: str, sheet_token: str) -> list[dict[str, Any]]:
    response = http_json(
        f"{FEISHU_BASE}/sheets/v3/spreadsheets/{urllib.parse.quote(sheet_token)}/sheets/query",
        headers=api_headers(token),
    )
    return response.get("data", {}).get("sheets", [])


def column_letter(number: int) -> str:
    result = ""
    while number > 0:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result or "A"


def is_date_header(header: str) -> bool:
    return "时间" in header or "日期" in header


def serial_to_datetime(value: str) -> str:
    try:
        serial = float(value)
    except (TypeError, ValueError):
        return value
    if serial < 1 or serial > 100000:
        return value
    date_value = datetime(1899, 12, 30) + timedelta(days=serial)
    if date_value.hour == 0 and date_value.minute == 0 and date_value.second == 0:
        return date_value.strftime("%Y-%m-%d")
    return date_value.strftime("%Y-%m-%d %H:%M:%S")


def normalize_rows(headers: list[str], rows: list[list[Any]]) -> list[list[Any]]:
    date_columns = {index for index, header in enumerate(headers) if is_date_header(header)}
    normalized: list[list[Any]] = []
    for row in rows:
        values = list(row)
        if len(values) < len(headers):
            values.extend([""] * (len(headers) - len(values)))
        for index in date_columns:
            if index < len(values) and values[index] not in (None, ""):
                values[index] = serial_to_datetime(str(values[index]))
        normalized.append(values)
    return normalized


def trim_table(headers: list[str], rows: list[list[Any]]) -> tuple[list[str], list[list[Any]]]:
    last_column = -1
    for index, value in enumerate(headers):
        if str(value or "").strip():
            last_column = index
    for row in rows:
        for index, value in enumerate(row):
            if value not in (None, "") and str(value).strip():
                last_column = max(last_column, index)
    if last_column < 0:
        return [], []
    width = last_column + 1
    trimmed_headers = list(headers[:width])
    trimmed_rows = []
    for row in rows:
        values = list(row[:width])
        if len(values) < width:
            values.extend([""] * (width - len(values)))
        if any(value not in (None, "") and str(value).strip() for value in values):
            trimmed_rows.append(values)
    return trimmed_headers, trimmed_rows


def read_sheet_values(
    token: str,
    sheet_token: str,
    sheet: dict[str, Any],
) -> tuple[list[str], list[list[Any]], int | None]:
    sheet_id = sheet.get("sheet_id")
    grid = sheet.get("grid_properties", {})
    total_rows = max(1, int(grid.get("row_count") or 1))
    total_columns = max(1, int(grid.get("column_count") or 1))
    end_column = column_letter(total_columns)
    all_rows: list[list[Any]] = []
    revision = None

    def fetch_chunk(bounds: tuple[int, int]) -> dict[str, Any]:
        start_row, end_row = bounds
        sheet_range = f"{sheet_id}!A{start_row}:{end_column}{end_row}"
        query = urllib.parse.urlencode(
            {
                "ranges": sheet_range,
                "valueRenderOption": "ToString",
            }
        )
        response = http_json(
            f"{FEISHU_BASE}/sheets/v2/spreadsheets/{urllib.parse.quote(sheet_token)}/values_batch_get?{query}",
            headers=api_headers(token),
        )
        return response

    chunk_bounds = [
        (start_row, min(total_rows, start_row + CHUNK_ROWS - 1))
        for start_row in range(1, total_rows + 1, CHUNK_ROWS)
    ]
    max_workers = min(CHUNK_CONCURRENCY, len(chunk_bounds))
    if max_workers <= 1:
        chunk_responses = [fetch_chunk(bounds) for bounds in chunk_bounds]
    else:
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="feishu-sheet") as executor:
            chunk_responses = list(executor.map(fetch_chunk, chunk_bounds))

    for response in chunk_responses:
        data = response.get("data", {})
        revision = data.get("revision", revision)
        ranges = data.get("valueRanges", [])
        values = ranges[0].get("values", []) if ranges else []
        all_rows.extend(values)
    if not all_rows:
        return [], [], revision
    headers = [str(value or "").strip() for value in all_rows[0]]
    rows = normalize_rows(headers, all_rows[1:])
    headers, rows = trim_table(headers, rows)
    return headers, rows, revision


def get_workbook_revision(
    token: str,
    sheet_token: str,
    sheet: dict[str, Any],
) -> int | None:
    sheet_id = sheet.get("sheet_id")
    if not sheet_id:
        return None
    query = urllib.parse.urlencode(
        {
            "ranges": f"{sheet_id}!A1:A1",
            "valueRenderOption": "ToString",
        }
    )
    response = http_json(
        f"{FEISHU_BASE}/sheets/v2/spreadsheets/{urllib.parse.quote(sheet_token)}/values_batch_get?{query}",
        headers=api_headers(token),
    )
    return response.get("data", {}).get("revision")


def resolve_mapping(sheets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_title = {str(sheet.get("title") or "").strip(): sheet for sheet in sheets}
    resolved: dict[str, dict[str, Any]] = {}
    missing = []
    for key, title in SHEET_MAPPING:
        sheet = by_title.get(title)
        if not sheet:
            matches = [candidate for name, candidate in by_title.items() if title in name]
            sheet = matches[0] if len(matches) == 1 else None
        if sheet:
            resolved[key] = sheet
        else:
            missing.append(title)
    if missing:
        available = "、".join(by_title.keys())
        raise ValueError(f"缺少子表：{'、'.join(missing)}。当前可用子表：{available}")
    return resolved


def cache_key(app_id: str, sheet_token: str) -> str:
    return hashlib.sha256(f"{app_id}:{sheet_token}".encode("utf-8")).hexdigest()[:20]


def cache_file(app_id: str, sheet_token: str) -> Path:
    return CACHE_DIR / f".feishu_sync_cache_{cache_key(app_id, sheet_token)}.json.gz"


def save_sync_cache(result: dict[str, Any], app_id: str, sheet_token: str) -> None:
    payload = {
        **result,
        "cachedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "stale": False,
    }
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = cache_file(app_id, sheet_token)
    temporary = target.with_suffix(".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=6) as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    temporary.replace(target)


def load_sync_cache(app_id: str, sheet_token: str) -> dict[str, Any] | None:
    target = cache_file(app_id, sheet_token)
    if target.exists():
        try:
            with gzip.open(target, "rt", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:
            return None
    if CACHE_DIR != ROOT:
        return None
    if not LEGACY_CACHE_FILE.exists():
        return None
    try:
        return json.loads(LEGACY_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def sync_feishu_workbook(
    app_id: str,
    app_secret: str,
    sheet_token_input: str,
    known_revision: int | None = None,
) -> dict[str, Any]:
    sync_started = time.perf_counter()
    sheet_token = parse_sheet_token(sheet_token_input)
    access_token = get_tenant_token(app_id, app_secret)
    spreadsheet = get_spreadsheet(access_token, sheet_token)
    sheets = get_sheets(access_token, sheet_token)
    mapping = resolve_mapping(sheets)
    latest_revision = get_workbook_revision(access_token, sheet_token, next(iter(mapping.values())))
    if known_revision is not None and latest_revision == known_revision:
        cached = load_sync_cache(app_id, sheet_token)
        if cached:
            cached_result = dict(cached)
            cached_result["code"] = 0
            cached_result["stale"] = False
            cached_result["unchanged"] = True
            cached_result["checkedAt"] = datetime.now().astimezone().isoformat(timespec="seconds")
            cached_result["syncStats"] = {
                "durationMs": round((time.perf_counter() - sync_started) * 1000),
                "cacheHit": True,
                "chunkConcurrency": CHUNK_CONCURRENCY,
            }
            return cached_result
    output_sheets: dict[str, Any] = {}
    sheet_durations: dict[str, float] = {}
    revision = None
    for key, sheet in mapping.items():
        sheet_started = time.perf_counter()
        headers, rows, sheet_revision = read_sheet_values(access_token, sheet_token, sheet)
        revision = sheet_revision if sheet_revision is not None else revision
        sheet_durations[key] = round(time.perf_counter() - sheet_started, 3)
        output_sheets[key] = {
            "sheetId": sheet.get("sheet_id"),
            "title": sheet.get("title"),
            "headers": headers,
            "rows": rows,
            "rowCount": len(rows),
            "columnCount": len(headers),
        }
        print(
            f"[feishu-sync] {sheet.get('title')}: {len(rows)} rows in {sheet_durations[key]:.1f}s",
            flush=True,
        )
    duration_ms = round((time.perf_counter() - sync_started) * 1000)
    result = {
        "code": 0,
        "spreadsheet": {
            "token": spreadsheet.get("token") or sheet_token,
            "title": spreadsheet.get("title") or "",
            "url": spreadsheet.get("url") or "",
            "revision": revision,
        },
        "sheets": output_sheets,
        "fetchedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "stale": False,
        "unchanged": False,
        "syncStats": {
            "durationMs": duration_ms,
            "sheetDurations": sheet_durations,
            "chunkConcurrency": CHUNK_CONCURRENCY,
        },
    }
    save_sync_cache(result, app_id, sheet_token)
    return result


class FeishuDashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self.send_response(302)
            self.send_header("Location", "/xjdashboardV3.3.10.html")
            self.end_headers()
            return
        if self.path == "/healthz":
            self.send_json(200, {"ok": True, "service": "xijiu-feishu-dashboard"})
            return
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        super().do_GET()

    def do_POST(self) -> None:
        if self.path != "/api/feishu/sync":
            self.send_error(404)
            return
        if not SYNC_SLOTS.acquire(blocking=False):
            self.send_json(429, {"code": "SYNC_BUSY", "message": "当前同步任务较多，请稍后重试"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 64 * 1024:
                raise ValueError("请求内容为空或过大")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            app_id = str(payload.get("appId") or "").strip()
            app_secret = str(payload.get("appSecret") or "").strip()
            sheet_token = str(payload.get("sheetToken") or "").strip()
            known_revision_value = payload.get("revision")
            known_revision = int(known_revision_value) if str(known_revision_value or "").isdigit() else None
            if not app_id or not app_secret or not sheet_token:
                raise ValueError("App ID、App Secret、Sheet Token 均不能为空")
            result = sync_feishu_workbook(app_id, app_secret, sheet_token, known_revision)
            self.send_json(200, result)
        except FeishuApiError as error:
            cached = load_sync_cache(app_id, parse_sheet_token(sheet_token))
            if cached and error.fallback_allowed:
                cached["code"] = 0
                cached["stale"] = True
                cached["syncWarning"] = f"实时读取失败，已加载上次成功数据：{error}"
                self.send_json(200, cached)
            else:
                self.send_json(
                    error.status,
                    {"code": error.code or "FEISHU_API_ERROR", "message": str(error)},
                )
        except Exception as error:
            self.send_json(400, {"code": "SYNC_ERROR", "message": str(error)})
        finally:
            SYNC_SLOTS.release()

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        use_gzip = len(body) > 100_000 and "gzip" in (self.headers.get("Accept-Encoding") or "")
        if use_gzip:
            body = gzip.compress(body, compresslevel=5)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        sys.stdout.write(
            "%s - - [%s] %s\n"
            % (self.client_address[0], self.log_date_time_string(), format % args)
        )

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        super().end_headers()


def main() -> None:
    os.chdir(ROOT)
    class DashboardServer(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True
        request_queue_size = 32

    server = DashboardServer((HOST, PORT), FeishuDashboardHandler)
    print(f"Feishu dashboard server: http://{HOST}:{PORT}/xjdashboardV3.3.10.html", flush=True)
    print(f"Health check: http://{HOST}:{PORT}/healthz", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
