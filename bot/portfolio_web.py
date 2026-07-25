"""KIS 보유자산 읽기 전용 로컬 웹 대시보드.

보안 경계:
  * 서버는 항상 127.0.0.1에만 바인딩한다.
  * GET/HEAD 외 HTTP 메서드는 허용하지 않는다.
  * 주문 모듈을 import하지 않고 ``bot.kis``의 조회 함수만 사용한다.
  * 계좌번호·API 키·토큰은 JSON이나 로그에 절대 포함하지 않는다.
  * 공개 GitHub Pages에는 이 서버가 배포되지 않는다.

실행:
  python -m bot.portfolio_web
  python -m bot.portfolio_web --port 8888
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "scanner" / "site_app"
PUBLIC_API = "https://easyseop.github.io/Stock-chart-analyze/api"
PUBLIC_FILES = frozenset(("signals.json", "paper_auto.json", "track.json"))
STATIC_FILES = frozenset((
    "index.html", "app.css", "portfolio_math.js", "app.js", "og.png", "og-v2.png",
))
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")
US_EXCHANGES = ("NASD", "NYSE", "AMEX")
PORTFOLIO_REFRESH_SECONDS = max(
    5, min(300, int(os.environ.get("PORTFOLIO_REFRESH_SECONDS", "60"))))
PORTFOLIO_BROWSER_REFRESH_SECONDS = max(
    3, min(30, int(os.environ.get("PORTFOLIO_BROWSER_REFRESH_SECONDS", "5"))))
PORTFOLIO_SHARED_CACHE_MAX_AGE_SECONDS = max(
    30, min(300, int(os.environ.get(
        "PORTFOLIO_SHARED_CACHE_MAX_AGE_SECONDS", "90"))))
_portfolio_lock = threading.Lock()
_portfolio_cache: tuple[float, dict] | None = None


def _position_meta(code: str) -> dict:
    """진입·손절 메타만 반환한다. 상태 파일 부재/손상은 빈 값으로 처리."""
    try:
        from bot import kis_positions
        row = kis_positions.load().get(code, {})
    except Exception:
        row = {}
    return {
        "entry": float(row.get("entry") or row.get("avg") or 0),
        "stop": float(row.get("stop") or 0),
        "target": float(row.get("target") or 0),
        "sleeve": str(row.get("sleeve") or "A"),
        "opened": str(row.get("opened") or ""),
    }


def portfolio_snapshot() -> dict:
    """국내·미국 보유를 중복 없이 합쳐 브라우저용 최소 필드로 정규화.

    명시적 진단/검증용 직접 KIS 조회다. 브라우저 HTTP 경로는 이 함수를 호출하지
    않고 ``cached_portfolio_snapshot``의 공유 캐시만 읽는다. 성공한 시장 결과는
    공유 캐시에 합쳐 이후 브라우저 요청이 KIS를 다시 부르지 않게 한다.
    """
    from bot import kis, market_cache

    seen: set[str] = set()
    rows: list[dict] = []
    failures: list[str] = []
    by_market: dict[str, list[dict]] = {"KR": [], "US": []}
    queries = [("KR", None)] + [("US", excg) for excg in US_EXCHANGES]
    for market, excg in queries:
        result = (kis.positions_detail("KR") if market == "KR"
                  else kis.positions_detail("US", excg=excg))
        if result is None:
            failures.append(market if excg is None else f"{market}:{excg}")
            continue
        for raw in result:
            code = str(raw.get("code") or "").upper()
            if not code or code in seen:
                continue
            seen.add(code)
            meta = _position_meta(code)
            normalized = {
                "code": code,
                "name": str(raw.get("name") or code),
                "market": str(raw.get("market") or market),
                "ccy": str(raw.get("ccy") or ("KRW" if market == "KR" else "USD")),
                "qty": max(0, int(float(raw.get("qty") or 0))),
                "avg": float(raw.get("avg") or 0),
                "cur": float(raw.get("cur") or 0),
                "eval_amt": float(raw.get("eval_amt") or 0),
                "buy_amt": float(raw.get("buy_amt") or 0),
                "pl_amt": float(raw.get("pl_amt") or 0),
                "pl_rt": float(raw.get("pl_rt") or 0),
                **meta,
            }
            rows.append(normalized)
            by_market[market].append(normalized)
    failed_markets = {item.split(":", 1)[0] for item in failures}
    for market in ("KR", "US"):
        if market not in failed_markets:
            try:
                market_cache.update_market(market, by_market[market])
            except Exception:
                pass                                  # 화면 캐시는 조회 결과를 망치지 않음
    rows.sort(key=lambda row: float(row["eval_amt"]), reverse=True)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": kis.ENV,
        "refresh_seconds": PORTFOLIO_REFRESH_SECONDS,
        "positions": rows,
        "partial": bool(failures),
        "failed_markets": failures,
        "read_only": True,
        "source": "kis_balance_fallback",
    }


def cached_portfolio_snapshot() -> dict:
    """공유 캐시만 읽는다. 웹 요청은 KIS REST를 직접 호출하지 않는다.

    파수꾼/매수루프가 갱신한 캐시가 없거나 낡으면 명시적인 unavailable 응답을
    돌려준다. 브라우저 새로고침이 KIS 호출량이나 주문 슬롯을 잠식하지 않는다.
    """
    try:
        from bot import kis, market_cache, settings
        shared = market_cache.portfolio(
            open_max_age=PORTFOLIO_SHARED_CACHE_MAX_AGE_SECONDS,
            market_open=lambda market: settings.market_open(
                "KRW" if market == "KR" else "USD"))
        if shared is not None:
            return {
                **shared,
                "environment": kis.ENV,
                "refresh_seconds": PORTFOLIO_BROWSER_REFRESH_SECONDS,
            }
    except Exception:
        pass
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": os.environ.get("KIS_ENV", "mock"),
        "refresh_seconds": PORTFOLIO_BROWSER_REFRESH_SECONDS,
        "positions": [],
        "partial": True,
        "failed_markets": ["shared_cache"],
        "read_only": True,
        "source": "shared_cache_unavailable",
        "message": "서버 공용 잔고 캐시를 기다리는 중입니다.",
    }


def chart_snapshot(code: str, days: int = 180) -> dict:
    """기존 스캐너의 일봉 캐시를 읽어 캔들·거래량·이동평균을 반환한다.

    매매 판단은 계산하지 않는다. 캐시가 없을 때만 기존 수집기를 호출하며, 결과는
    스캐너가 원래 사용하던 로컬 캐시에 저장된다. 이동평균은 표시 전용이다.
    """
    symbol = str(code).strip().upper()
    if not SYMBOL_RE.fullmatch(symbol):
        raise ValueError("invalid symbol")
    days = max(20, min(365, int(days)))
    from scanner import cache
    frames = cache.frames(symbol, refresh=False)
    daily = frames["D"].tail(max(days, 120))
    closes: list[float] = []
    all_points: list[dict] = []
    for idx, row in daily.iterrows():
        close = float(row.get("Close") or 0)
        if not math.isfinite(close) or close <= 0:
            continue
        closes.append(close)

        def price(key: str, fallback: float) -> float:
            try:
                value = float(row.get(key))
                return value if math.isfinite(value) and value > 0 else fallback
            except (TypeError, ValueError):
                return fallback

        def ma(period: int):
            if len(closes) < period:
                return None
            return round(sum(closes[-period:]) / period, 4)

        all_points.append({
            "date": idx.strftime("%Y-%m-%d"),
            "open": round(price("Open", close), 4),
            "high": round(price("High", close), 4),
            "low": round(price("Low", close), 4),
            "close": round(close, 4),
            "volume": max(0, int(price("Volume", 0))),
            "ma20": ma(20),
            "ma60": ma(60),
            "ma120": ma(120),
        })
    points = all_points[-days:]
    return {
        "code": symbol,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "points": points,
        "source": "existing-scanner-cache",
        "interval": "1d",
        "read_only": True,
    }


def quote_snapshot(code: str, limit: int = 900) -> dict:
    """파수꾼이 이미 읽은 보유종목 현재가 이력. 이 경로는 KIS를 호출하지 않는다."""
    symbol = str(code).strip().upper()
    if not SYMBOL_RE.fullmatch(symbol):
        raise ValueError("invalid symbol")
    from bot import market_cache
    return market_cache.quote_history(symbol, limit=limit)


def performance_snapshot() -> dict:
    """KIS 전략 성과와 지수 비교. 금액·수량 없이 퍼센트만 반환한다."""
    from bot import alpha, kis
    payload = alpha.dashboard_snapshot()
    return {
        **payload,
        "environment": kis.ENV,
        "read_only": True,
    }


def trade_history_snapshot(limit: int = 200) -> dict:
    """확정 체결 로컬 원장만 읽는 거래이력. KIS·주문 모듈 호출은 없다."""
    from bot import trade_history
    return trade_history.snapshot(limit=limit)


def _public_json(name: str) -> bytes:
    if name not in PUBLIC_FILES:
        raise FileNotFoundError(name)
    req = urllib.request.Request(
        f"{PUBLIC_API}/{name}",
        headers={"User-Agent": "Stock-chart-analyze-local-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=15) as response:
        return response.read()


class PortfolioHandler(BaseHTTPRequestHandler):
    server_version = "PortfolioReadOnly/1.0"

    def _headers(self, status: int, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self._headers(status, content_type, len(body))
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self._send(status, body, "application/json; charset=utf-8")

    def _static(self, name: str) -> None:
        if name not in STATIC_FILES:
            self._json(404, {"error": "not_found"})
            return
        path = APP_DIR / name
        if not path.is_file():
            self._json(404, {"error": "asset_missing"})
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type == "application/javascript":
            content_type += "; charset=utf-8"
        self._send(200, path.read_bytes(), content_type)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path in ("/", "/app", "/app/", "/app/index.html"):
                self._static("index.html")
                return
            if path.startswith("/app/"):
                self._static(path.rsplit("/", 1)[-1])
                return
            if path == "/api/portfolio.json":
                self._json(200, cached_portfolio_snapshot())
                return
            if path == "/api/chart.json":
                q = parse_qs(parsed.query)
                code = (q.get("code") or [""])[0]
                days = int((q.get("days") or ["180"])[0])
                self._json(200, chart_snapshot(code, days))
                return
            if path == "/api/quotes.json":
                q = parse_qs(parsed.query)
                code = (q.get("code") or [""])[0]
                limit = int((q.get("limit") or ["900"])[0])
                self._json(200, quote_snapshot(code, limit))
                return
            if path == "/api/performance.json":
                self._json(200, performance_snapshot())
                return
            if path == "/api/trades.json":
                q = parse_qs(parsed.query)
                limit = int((q.get("limit") or ["200"])[0])
                self._json(200, trade_history_snapshot(limit))
                return
            if path.startswith("/api/"):
                name = path.rsplit("/", 1)[-1]
                body = _public_json(name)
                self._send(200, body, "application/json; charset=utf-8")
                return
            self._json(404, {"error": "not_found"})
        except ValueError as exc:
            self._json(400, {"error": "bad_request", "message": str(exc)})
        except Exception as exc:
            self._json(503, {
                "error": "read_failed",
                "message": f"{type(exc).__name__}: 조회하지 못했습니다."})

    def do_HEAD(self) -> None:
        self.do_GET()

    def _read_only(self) -> None:
        self._json(405, {"error": "read_only"})

    do_POST = _read_only
    do_PUT = _read_only
    do_PATCH = _read_only
    do_DELETE = _read_only

    def log_message(self, fmt: str, *args) -> None:
        # 쿼리스트링이나 응답 내용을 남기지 않는다.
        print(f"[portfolio] {self.command} {urlparse(self.path).path} → {args[1]}")


def serve(port: int = 8888) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", int(port)), PortfolioHandler)
    print(f"읽기 전용 자산 대시보드: http://127.0.0.1:{port}/app/")
    print("종료: Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="KIS 읽기 전용 로컬 자산 대시보드")
    parser.add_argument("--port", type=int, default=8888)
    args = parser.parse_args()
    serve(args.port)


if __name__ == "__main__":
    main()
