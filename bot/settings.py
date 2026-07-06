"""실행 봇 설정 — 안전장치가 기본값. 실계좌 전환 전 반드시 페이퍼로 충분히 검증.

두뇌/손 분리 아키텍처:
  [두뇌] GitHub Actions 스캐너(15분) → /api/signals.json (일봉 신호 — 느려도 됨)
  [손]   이 봇(사용자 PC/서버)      → 실시간 시세로 진입/손절 방아쇠 (빨라야 함)
"""

# ── 시그널 소스 ──────────────────────────────────────────────────
SIGNALS_URL = "https://easyseop.github.io/Stock-chart-analyze/api/signals.json"
SITE_URL = "https://easyseop.github.io/Stock-chart-analyze"
HOLDINGS_EDIT_URL = ("https://github.com/easyseop/Stock-chart-analyze/edit/"
                     "claude/korean-text-review-o3wmsv/holdings.json")

# ── 계좌/리스크 ──────────────────────────────────────────────────
ACCOUNT_KRW = 10_000_000     # 봇 운용 자금(원). 수량 = 1회 리스크 예산 / 주당 손절폭
RISK_PER_TRADE = 0.01        # 1회 매매 손실 허용 = 계좌의 1%
POS_CAP_FRACTION = 1.0 / 3   # 종목당 최대 비중 = 계좌의 1/3(사용자 확정).
                             #   autopaper(모의)와 동일 규칙 — 실거래도 한 종목에
                             #   시드의 1/3 초과 투입 금지. 리스크·비중 중 빡빡한 쪽.
FX_USDKRW = 1380.0           # 달러 환산(달러 시그널 수량 계산용)

# ── 안전 가드(사용자와 합의한 핵심) ─────────────────────────────
ENTRY_TOLERANCE = 0.015      # 실시간가가 시그널 진입가 ±1.5% 이내일 때만 매수
                             #   (신호 계산 시점과 주문 시점의 가격 괴리 차단)
CONFIRMED_ONLY = True        # 확정봉 모드: 전 거래일 시그널에도 있던 종목만 매수
                             #   (미확정 일봉의 가짜 돌파가 종가에 뒤집히는 것 방지)
MAX_POSITIONS = 5            # 동시 보유 최대 종목 수
DAILY_LOSS_LIMIT = 0.02      # 하루 실현손실이 계좌의 2% 넘으면 당일 신규 매수 중지
TRADE_GROUPS = ("now",)      # 매매 대상 그룹 — 'now'(지금 진입)만. watch는 관찰용.

# ── 실행 모드 ────────────────────────────────────────────────────
BROKER = "paper"             # 'paper'=모의 체결(기본) / 'toss'=토스 API(키 필요, 미구현)
POLL_SEC = 300               # --loop 모드 폴링 주기(초)

# ── 상태 파일(봇 로컬 저장 — git 추적 안 함) ────────────────────
STATE_PATH = "bot/state.json"
SEEN_PATH = "bot/seen.json"

# ── 한국시간(KST) — 날짜 스탬프/일지 기준(러너가 UTC라도 한국 시계로) ──
import datetime as _dt
KST = _dt.timezone(_dt.timedelta(hours=9))


def today_kst() -> str:
    return _dt.datetime.now(KST).date().isoformat()


def days_ago_kst(n: int) -> str:
    return (_dt.datetime.now(KST).date() - _dt.timedelta(days=n)).isoformat()


def market_open(ccy: str) -> bool:
    """지금 해당 시장이 장중인가 — 알림을 '지금 행동 가능한' 종목만 보내려는 필터.
    autopaper._market_open과 동일 규칙(둘 다 바꿀 것).
      한국주: 평일 00:00~06:30 UTC(=09:00~15:30 KST)
      미국주: 미 동부시간(ET) 평일 09:30~16:00 — 서머타임 정확 반영
    """
    now = _dt.datetime.now(_dt.timezone.utc)
    if ccy == "KRW":
        if now.weekday() >= 5:
            return False
        hm = now.hour * 60 + now.minute
        return 0 <= hm <= 390
    try:
        from zoneinfo import ZoneInfo
        et = now.astimezone(ZoneInfo("America/New_York"))
        if et.weekday() >= 5:
            return False
        hm = et.hour * 60 + et.minute
        return 570 <= hm < 960
    except Exception:                 # tzdata 없으면 기존 고정창 폴백
        if now.weekday() >= 5:
            return False
        hm = now.hour * 60 + now.minute
        return 810 <= hm <= 1260


# 알림 폭주 방지 — 한 실행에서 보낼 최대 개수(우선순위 높은 것부터)
BUY_ALERT_MAX = 8
ARRIVAL_ALERT_MAX = 6
# 하루 총량 상한(매수+도달 제안) — 회당 상한이 깨져도 누적 폭주는 막는 서킷브레이커.
#   미국장 밤새 ~13회 실행 × 회당 최대 14 = 이론상 180+ → 40으로 캡.
DAILY_ALERT_BUDGET = 40
