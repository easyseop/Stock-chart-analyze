"""브라우저 QA 전용 미리보기 서버.

프로덕션 산출물에는 포함되지 않는다. 아래 URL로 정적 UI의 상태를 재현한다.
  /fresh/app/      정상 신호
  /stale/app/      15분 이상 지난 신호
  /empty/app/      신호 0건
  /error/app/      신호 API 오류
  /portfolio/app/  로컬 보유자산 + 차트
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "scanner" / "site_app"
STATES = {"fresh", "stale", "empty", "error", "portfolio"}


def _signals(state: str) -> dict:
    generated = datetime.now(timezone.utc)
    if state == "stale":
        generated -= timedelta(minutes=37)
    signals = [] if state == "empty" else [
        {
            "id": "AAPL-preview-now", "code": "AAPL", "name": "Apple",
            "ccy": "USD", "group": "now", "entry_kind": "now", "stage": 4,
            "price": 214.42, "entry": 213.80, "stop": 201.00, "target": 238.50,
            "shares_1pct": 7, "range_pos": .43, "norm": 71.2, "bear_share": .08,
            "fresh": True, "break_gap": 0, "earnings_d": 18,
            "tactic": {
                "mode": "half", "label": "⚖ 반반 진입",
                "desc": "손절폭 6.0% — 절반은 현재가, 절반은 눌림 $208.20 지정가",
                "stop_pct": 6.0, "pb_price": 208.20, "pb_stop_pct": 5.0,
            },
        },
        {
            "id": "005930-preview-now", "code": "005930", "name": "삼성전자",
            "ccy": "KRW", "group": "now", "entry_kind": "pullback", "stage": 3,
            "price": 80300, "entry": 79800, "stop": 75400, "target": 89200,
            "shares_1pct": 18, "range_pos": .31, "norm": 63.4, "bear_share": .11,
            "fresh": False, "break_gap": .006, "earnings_d": None,
            "tactic": {
                "mode": "pullback", "label": "⌄ 눌림 진입",
                "desc": "현재가를 추격하지 않고 계획한 눌림 가격에서만 진입",
                "stop_pct": 5.5, "pb_price": 79800, "pb_stop_pct": 5.5,
            },
        },
        {
            "id": "VRSK-preview-watch", "code": "VRSK", "name": "Verisk Analytics",
            "ccy": "USD", "group": "watch", "entry_kind": "breakout", "stage": 2,
            "price": 188.20, "entry": 191.00, "stop": 178.00, "target": 211.00,
            "shares_1pct": 6, "range_pos": .28, "norm": 58.0, "bear_share": .16,
            "fresh": False, "break_gap": .014, "earnings_d": 26,
            "tactic": {"mode": "full", "label": "● 확인 후 진입",
                       "desc": "돌파 확인 전에는 관찰만 유지"},
        },
        {
            "id": "MSFT-preview-shelf", "code": "MSFT", "name": "Microsoft",
            "ccy": "USD", "group": "shelf", "entry_kind": "shelf", "stage": 3,
            "price": 442.35, "entry": 440.00, "stop": 421.00, "target": 478.00,
            "shares_1pct": 5, "range_pos": .52, "norm": 65.0, "bear_share": .09,
            "fresh": True, "break_gap": 0, "earnings_d": 30,
            "tactic": {"mode": "full", "label": "▤ 매물대 반등",
                       "desc": "밸류영역 회복을 확인한 전략 B 후보"},
            "shelf": {"poc": 435.1, "val": 421.3, "vah": 451.8,
                      "rr": 1.81, "overhead": .21,
                      "checks": {"터치": True, "회복": True, "상단마감": True,
                                 "거래량": True, "신저가아님": True}},
        },
        {
            "id": "LOW-preview-shelf-watch", "code": "LOW", "name": "Lowe's",
            "ccy": "USD", "group": "shelf_watch", "entry_kind": "shelf_watch",
            "stage": 0, "price": 221.40, "entry": 221.40,
            "stop": 211.20, "target": 239.10, "shares_1pct": 0,
            "range_pos": .27, "norm": 57.4, "bear_share": .18,
            "fresh": False, "break_gap": None, "earnings_d": 22,
            "tactic": "watch",
            "shelf": {"poc": 219.2, "val": 212.7, "vah": 239.1,
                      "rr": 1.74, "overhead": .31,
                      "reason": "반등 미확인(거래량·상단마감)",
                      "checks": {"터치": True, "회복": True, "상단마감": False,
                                 "거래량": False, "신저가아님": True}},
        },
    ]
    return {
        "version": 1, "generated_at": generated.isoformat(),
        "note": "차트 기반 시그널 — 주문 전 가격·체결가능성 재확인 필수. 투자권유 아님.",
        "signals": signals,
    }


PAPER = {
    "updated": datetime.now(timezone.utc).isoformat(),
    "cash": 61_420_000, "equity": 104_820_000, "ret_pct": 4.82,
    "positions": [], "pending": [], "trades": 17, "win_trades": 10,
    "pos_cap_pct": 33.3, "cap_violations": [], "rule_violations": [],
}
TRACK = {"generated_at": datetime.now(timezone.utc).isoformat(),
         "stats": {"win_rate": 58.8, "avg_r": .74}, "recent": []}
PORTFOLIO = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "environment": "mock", "partial": False, "failed_markets": [],
    "read_only": True, "refresh_seconds": 5, "price_age_seconds": 3,
    "source": "sentinel_shared_cache",
    "positions": [
        {"code": "AAPL", "name": "Apple", "market": "US", "ccy": "USD",
         "qty": 12, "avg": 189.40, "cur": 214.42, "eval_amt": 2573.04,
         "buy_amt": 2272.80, "pl_amt": 300.24, "pl_rt": 13.21,
         "entry": 189.40, "stop": 201.00, "target": 215.50, "sleeve": "A",
         "opened": "2026-07-18"},
        {"code": "005930", "name": "삼성전자", "market": "KR", "ccy": "KRW",
         "qty": 24, "avg": 75200, "cur": 80300, "eval_amt": 1927200,
         "buy_amt": 1804800, "pl_amt": 122400, "pl_rt": 6.78,
         "entry": 75200, "stop": 79800, "target": 89000, "sleeve": "B",
         "opened": "2026-07-22"},
        {"code": "NVDA", "name": "NVIDIA", "market": "US", "ccy": "USD",
         "qty": 2, "avg": 180.00, "cur": 173.00, "eval_amt": 346.00,
         "buy_amt": 360.00, "pl_amt": -14.00, "pl_rt": -3.89,
         "entry": 180.00, "stop": 0, "target": 0, "sleeve": "A"},
    ],
}

PERFORMANCE = {
    "version": 3, "generated_at": datetime.now(timezone.utc).isoformat(),
    "sample_seconds": 300,
    "basis": "KIS 봇 보유 NAV/TWR · 매매 현금흐름 제거 · 미국은 환율 포함",
    "markets": {
        "US": {"label": "미국", "date": "2026-07-24",
               "basis": "previous_close",
               "indices": ["나스닥", "S&P500"], "series": []},
        "KR": {"label": "한국", "date": "2026-07-24",
               "basis": "previous_close",
               "indices": ["코스피", "코스닥"], "series": []},
    },
    "days": [], "environment": "mock", "read_only": True,
}
for market, index_names in (("US", ["나스닥", "S&P500"]),
                            ("KR", ["코스피", "코스닥"])):
    for i in range(24):
        PERFORMANCE["markets"][market]["series"].append({
            "t": f"{9 + i // 12:02d}:{(i % 12) * 5:02d}",
            "account": round(i * .035 + ((i % 5) - 2) * .04, 3),
            "A": round(i * .041, 3), "B": round(i * .018 - .12, 3),
            "holdings": {"account": round(i * .03 + .11, 3),
                         "A": round(i * .035, 3), "B": round(i * .016, 3),
                         "covered": 7, "eligible": 8},
            "indices": {
                index_names[0]: round(i * .025, 3),
                index_names[1]: round(i * .019 - .05, 3),
            },
            "daily_indices": {
                index_names[0]: round(i * .025, 3),
                index_names[1]: round(i * .019 - .05, 3),
            },
        })

TRADES = {
    "version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
    "read_only": True, "available": True, "partial": False,
    "source": "confirmed-local-fill-journals",
    "summary": {
        "sell_fills": 3, "wins": 2, "losses": 1,
        "win_rate": 66.67, "realized_pnl_krw": 218400,
    },
    "message": "확정 체결 이후의 로컬 원장 기록만 표시합니다. 원장 도입 전 과거 거래는 추정하지 않습니다.",
    "trades": [
        {
            "executed_at": "2026-07-25T10:34:00+09:00", "day": "2026-07-25",
            "code": "ALK", "name": "Alaska Air Group", "market": "US",
            "ccy": "USD", "sleeve": "A", "reason": "하드 손절(손절가 이탈)",
            "reason_kind": "stop", "qty": 8, "entry_price": 62.40,
            "exit_price": 58.10, "price_pnl": -34.40,
            "realized_pnl_krw": -47200, "return_pct": -6.91,
            "remaining_qty": 0, "partial_exit": False, "verified": True,
        },
        {
            "executed_at": "2026-07-24T14:18:00+09:00", "day": "2026-07-24",
            "code": "005930", "name": "삼성전자", "market": "KR",
            "ccy": "KRW", "sleeve": "B", "reason": "B 목표(VAH) 도달",
            "reason_kind": "take_profit", "qty": 12, "entry_price": 75200,
            "exit_price": 82100, "price_pnl": 82800,
            "realized_pnl_krw": 81600, "return_pct": 9.04,
            "remaining_qty": 12, "partial_exit": True, "verified": True,
        },
        {
            "executed_at": "2026-07-23T22:11:00+09:00", "day": "2026-07-23",
            "code": "AAPL", "name": "Apple", "market": "US",
            "ccy": "USD", "sleeve": "A", "reason": "익절 +1R 절반",
            "reason_kind": "take_profit", "qty": 6, "entry_price": 189.40,
            "exit_price": 214.20, "price_pnl": 148.80,
            "realized_pnl_krw": 184000, "return_pct": 13.11,
            "remaining_qty": 6, "partial_exit": True, "verified": True,
        },
    ],
}


def _chart(code: str) -> dict:
    base = 188 if code == "AAPL" else 166 if code == "NVDA" else 72000
    step = 1.8 if code == "AAPL" else 1.45 if code == "NVDA" else 610
    points = []
    start = datetime(2026, 1, 2, tzinfo=timezone.utc)
    for i in range(120):
        wave = ((i % 13) - 6) * step * .33
        close = round(base + i * step * .19 + wave, 2)
        opened = close - step * .4
        points.append({
            "date": (start + timedelta(days=i)).strftime("%Y-%m-%d"),
            "open": round(opened, 2), "high": round(max(opened, close) + step, 2),
            "low": round(min(opened, close) - step, 2), "close": close,
            "volume": 1_000_000 + i * 12000,
            "ma20": close - step * .8 if i >= 19 else None,
            "ma60": close - step * 1.7 if i >= 59 else None,
            "ma120": close - step * 2.4 if i >= 119 else None,
        })
    return {"code": code, "source": "preview-fixture", "interval": "1d",
            "read_only": True, "points": points}


def _quotes(code: str) -> dict:
    now = datetime.now(timezone.utc).timestamp()
    base = 214.0 if code == "AAPL" else 173.0 if code == "NVDA" else 80300.0
    step = .12 if code == "AAPL" else .16 if code == "NVDA" else 18.0
    return {
        "code": code, "source": "sentinel_shared_cache", "read_only": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "points": [{"ts": now - (59 - i) * 20,
                    "price": round(base + ((i % 9) - 4) * step + i * step * .04, 2)}
                   for i in range(60)],
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in (
                "/qa-390.html", "/qa-320.html",
                "/qa-390-detail.html", "/qa-320-detail.html",
                "/qa-390-history.html", "/qa-320-history.html"):
            width = 390 if "390" in parsed.path else 320
            if "detail" in parsed.path:
                action_script = """
<script>const frame=document.querySelector("iframe");
frame.addEventListener("load",()=>setTimeout(()=>
  frame.contentDocument.querySelector(".portfolio-card")?.click(),700));</script>
"""
            elif "history" in parsed.path:
                action_script = """
<script>const frame=document.querySelector("iframe");
frame.addEventListener("load",()=>setTimeout(()=>
  frame.contentDocument.querySelector('[data-portfolio-mode="history"]')?.click(),700));</script>
"""
            else:
                action_script = ""
            body = f"""<!doctype html><meta charset="utf-8">
<title>{width}px responsive QA</title>
<style>html,body{{margin:0;background:#dfe4ec}}iframe{{display:block;width:{width}px;
height:844px;margin:0 auto;border:0;background:white}}</style>
<iframe src="/portfolio/app/#portfolio" title="{width}px responsive QA"></iframe>
{action_script}""".encode()
            self._send(200, body, "text/html; charset=utf-8")
            return
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2 or parts[0] not in STATES:
            self._json(404, {"error": "preview_path"})
            return
        state, section = parts[0], parts[1]
        if section == "app":
            asset = parts[2] if len(parts) > 2 else "index.html"
            if asset not in {
                "index.html", "app.css", "portfolio_math.js", "app.js",
                "og.png", "og-v2.png",
            }:
                self._json(404, {"error": "asset"})
                return
            path = APP / asset
            kinds = {".html": "text/html", ".css": "text/css",
                     ".js": "application/javascript", ".png": "image/png"}
            self._send(200, path.read_bytes(), kinds[path.suffix])
            return
        if section != "api" or len(parts) < 3:
            self._json(404, {"error": "endpoint"})
            return
        name = parts[2]
        if name == "signals.json":
            if state == "error":
                self._json(503, {"error": "preview_failure"})
            else:
                self._json(200, _signals(state))
        elif name == "paper_auto.json":
            self._json(200, PAPER)
        elif name == "track.json":
            self._json(200, TRACK)
        elif name == "portfolio.json":
            if state == "portfolio":
                self._json(200, PORTFOLIO)
            else:
                self._json(404, {"error": "local_only"})
        elif name == "chart.json" and state == "portfolio":
            code = (parse_qs(parsed.query).get("code") or ["AAPL"])[0]
            self._json(200, _chart(code))
        elif name == "quotes.json" and state == "portfolio":
            code = (parse_qs(parsed.query).get("code") or ["AAPL"])[0]
            self._json(200, _quotes(code))
        elif name == "performance.json" and state == "portfolio":
            self._json(200, PERFORMANCE)
        elif name == "trades.json" and state == "portfolio":
            self._json(200, TRADES)
        else:
            self._json(404, {"error": "endpoint"})

    def log_message(self, _format: str, *_args) -> None:
        pass


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 8877), Handler)
    print("QA preview: http://127.0.0.1:8877/fresh/app/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
