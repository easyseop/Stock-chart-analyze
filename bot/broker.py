"""브로커 어댑터 — 봇의 '손'. 페이퍼(모의 체결)가 기본, 토스는 API 키 확보 후 구현.

인터페이스만 맞추면 어느 증권사든 갈아끼울 수 있게 얇게 유지한다:
  quote(code, ccy)      → 현재가(실시간이면 실시간, 페이퍼는 최근 종가/시그널가)
  buy(code, qty, price) → 주문(페이퍼는 즉시 체결로 기록)
  sell(code, qty, price)
"""
from __future__ import annotations


class PaperBroker:
    """모의 체결 — 주문을 실제로 내지 않고 지정가 그대로 체결됐다고 기록.

    시세: FinanceDataReader 최근 일봉 종가(지연). 실시간이 아니므로 페이퍼 검증
    전용 — 실계좌 정확도는 토스 어댑터(실시간 웹소켓)에서 확보한다.
    offline=True면 네트워크 없이 시그널가를 그대로 사용(로직 테스트용).
    """

    def __init__(self, offline: bool = False):
        self.offline = offline
        self._px_cache: dict[str, float] = {}

    def quote(self, code: str, ccy: str, fallback: float | None = None):
        if self.offline:
            return fallback
        if code in self._px_cache:
            return self._px_cache[code]
        try:
            import FinanceDataReader as fdr
            df = fdr.DataReader(code)
            px = float(df["Close"].iloc[-1])
            self._px_cache[code] = px
            return px
        except Exception:
            return fallback

    def buy(self, code: str, qty: int, price: float) -> dict:
        return {"ok": True, "filled": qty, "price": price, "paper": True}

    def sell(self, code: str, qty: int, price: float) -> dict:
        return {"ok": True, "filled": qty, "price": price, "paper": True}


class TossBroker:
    """토스증권 OpenAPI 어댑터 — 자리만. API 키(앱키/시크릿) 발급 후 구현.

    구현 시 채울 것:
      - 인증(토큰 발급/갱신)
      - quote(): 실시간 시세(웹소켓 구독 또는 REST 폴링)
      - buy()/sell(): 주문 API + 체결 확인
      - 예수금/보유 조회로 state 동기화
    """

    def __init__(self, app_key: str | None = None, app_secret: str | None = None):
        raise NotImplementedError(
            "토스 API 연동 미구현 — 앱 키 발급 후 이 어댑터를 채우세요. "
            "그 전까지는 BROKER='paper'로 검증.")


def make(name: str, offline: bool = False):
    if name == "paper":
        return PaperBroker(offline=offline)
    if name == "toss":
        return TossBroker()
    raise ValueError(f"모르는 브로커: {name}")
