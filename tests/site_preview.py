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
                      "rr": 1.81, "overhead": .21},
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
    "read_only": True,
    "positions": [
        {"code": "AAPL", "name": "Apple", "market": "US", "ccy": "USD",
         "qty": 12, "avg": 189.40, "cur": 214.42, "eval_amt": 2573.04,
         "buy_amt": 2272.80, "pl_amt": 300.24, "pl_rt": 13.21,
         "entry": 189.40, "stop": 201.00, "sleeve": "A"},
        {"code": "005930", "name": "삼성전자", "market": "KR", "ccy": "KRW",
         "qty": 24, "avg": 75200, "cur": 80300, "eval_amt": 1927200,
         "buy_amt": 1804800, "pl_amt": 122400, "pl_rt": 6.78,
         "entry": 75200, "stop": 75400, "sleeve": "A"},
    ],
}


def _chart(code: str) -> dict:
    base = 188 if code == "AAPL" else 72000
    step = 1.8 if code == "AAPL" else 610
    points = []
    start = datetime(2026, 1, 2, tzinfo=timezone.utc)
    for i in range(120):
        wave = ((i % 13) - 6) * step * .33
        points.append({
            "date": (start + timedelta(days=i)).strftime("%Y-%m-%d"),
            "close": round(base + i * step * .19 + wave, 2),
        })
    return {"code": code, "source": "preview-fixture", "points": points}


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
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2 or parts[0] not in STATES:
            self._json(404, {"error": "preview_path"})
            return
        state, section = parts[0], parts[1]
        if section == "app":
            asset = parts[2] if len(parts) > 2 else "index.html"
            if asset not in {"index.html", "app.css", "app.js", "og.png"}:
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
