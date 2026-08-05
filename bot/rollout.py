"""I7 — 단계적 롤아웃 가드(코드 강제) + I1 세션 게이트(US 정규장만).

문서가 아니라 **코드가** Stage 제한을 강제한다(리뷰 I7 채택). 신규 매수 앞에서
X1이 이 모듈의 check_new_entry()를 반드시 통과해야 한다.

Stage 프로파일(환경변수 TRADE_STAGE, 기본 "1.5"):
  1.5    모의 검증:  1종목 · 하루 1건 · risk ≤0.1% · allowlist 필수 · whole-share
  2      실전 첫 주: 1종목 · 하루 1건 · risk ≤0.1% · allowlist 필수 · US 정규장만
  2.5    확장:      3종목 · 하루 2건 · risk ≤0.25%
  3      정상:      5종목 · 하루 3건 · risk ≤1.0%
  mirror KIS 스캐너 직접진입(mock — key 이름은 legacy alias, autopaper
         미러 아님): 동시 보유 종목 수 제한 없음 · 하루 10건 · risk ≤1.0% ·
         **수동 종목 allowlist 없음**. 신선한 스캐너 신호를 KIS 시세로 직접 집행하며,
         신규 진입 가능 여부는 고정 종목 수가 아니라 슬리브 예산, A+B 통합
         운용한도, KIS 매수여력과 계산 수량으로 결정한다. 모의 전용으로
         안전하다(live는 kis_orders가 Stage 2 게이트 전 하드블록).

공통 강제(전 Stage):
  · **각 시장 정규장만**(I1·Codex B7) — pre/after/dayMarket 신규 진입 hard-off.
  · whole-share만(소수점 주문 가능 여부 [대조필요]라 금지).
  · 1.5·2·2.5 Stage는 ALLOWED_SYMBOLS(콤마 구분) 밖 종목 금지. mirror는
    스캐너의 유효 진입 후보 전체를 대상으로 하므로 수동 목록을 요구하지 않는다.
    단, 비어 있지 않은 목록을 운영자가 긴급 축소용으로 명시하면 그 목록을 지킨다.
  · 하루 신규 카운트는 원장(submit·side=BUY·당일)에서 계산 — 별도 상태 파일 없음
    (재시작에도 정확).
"""
from __future__ import annotations

import datetime
import os

from bot import ledger

_PROFILES = {
    "1.5": {"max_positions": 1, "max_new_per_day": 1, "risk_cap": 0.001,
            "allowlist_required": True},
    "2":   {"max_positions": 1, "max_new_per_day": 1, "risk_cap": 0.001,
            "allowlist_required": True},
    "2.5": {"max_positions": 3, "max_new_per_day": 2, "risk_cap": 0.0025,
            "allowlist_required": True},
    "3":   {"max_positions": 5, "max_new_per_day": 3, "risk_cap": 0.01,
            "allowlist_required": False},
    # KIS 스캐너 직접진입 limited mock 프로필(2026-08-05 정정 — key 이름 'mirror'는
    #   legacy alias, autopaper 미러 의미 아님). 동시 보유 수는 제한하지 않고
    #   실제 신규 투입은 envelope의 슬리브 예산·A+B 통합 운용한도·브로커
    #   매수여력·종목별 1/3·risk 1% 중 가장 작은 값으로 제한한다.
    #   사용자가 확정한 독립계좌 계약에 따라 수동 종목 allowlist는 요구하지
    #   않는다. 신선도·전략계약·KIS 시세·잔고·예산·세션 게이트가 후보를 제한한다.
    #   비어 있지 않은 목록을 별도로 주면 비상 축소용 optional fence로는 동작한다.
    #   하루 10건은 autopaper 3건의 복제가 아니라 주문 폭주 방지용 KIS 독립 상한.
    "mirror": {"max_positions": None, "max_new_per_day": 10, "risk_cap": 0.01,
               "allowlist_required": False},
}


def stage() -> str:
    s = os.environ.get("TRADE_STAGE", "1.5")
    return s if s in _PROFILES else "1.5"


def profile() -> dict:
    return dict(_PROFILES[stage()])


# git 추적 allowlist 파일 — 시크릿이 아니므로(심볼은 공개 피드에 이미 있음) 저장소로
#   관리해 자동배포로 갱신한다. env ALLOWED_SYMBOLS가 있으면 그것이 항상 이긴다
#   (운영자 즉시 override — 서버에서 git 없이 조일 수 있게).
_ALLOWLIST_FILE_DEFAULT = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "allowed_symbols.txt"))


def _allowlist_from_file() -> set[str] | None:
    """파일 allowlist(줄/콤마 구분, '#' 주석). 파일 없음=None(미설정).
    파일이 **있는데 심볼 0개**면 빈 set = 전 종목 거부 — 편집 실수를 fail-closed로.
    울타리를 의도적으로 없애려면 파일 자체를 지운다(git 이력에 남는 결정)."""
    path = os.environ.get("ALLOWED_SYMBOLS_FILE", _ALLOWLIST_FILE_DEFAULT)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    syms: set[str] = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0]
        for token in line.replace(",", " ").split():
            syms.add(token.upper())
    return syms


def allowed_symbols() -> set[str] | None:
    """env ALLOWED_SYMBOLS(콤마 구분) 우선, **미설정일 때만** git 추적 파일.
    둘 다 없으면 None(allowlist_required Stage면 전 거부).

    env가 **존재하면** 값이 비었거나 공백뿐이어도 그 값이 확정이다 — 빈 env가
    낡은 파일로 폴백하면 운영자가 빈 값으로 전 종목을 잠갔다고 믿는 동안
    파일이 매수를 되살린다(Codex P1-1). 빈 env = 빈 set = 전 종목 거부."""
    if "ALLOWED_SYMBOLS" in os.environ:
        raw = os.environ["ALLOWED_SYMBOLS"]
        return {s.strip().upper() for s in raw.split(",") if s.strip()}
    return _allowlist_from_file()


def _today_kst() -> datetime.date:
    return datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=9))).date()


def new_entries_today() -> int:
    """오늘(KST) 원장에 기록된 신규 매수 submit 수 — 하루 한도 계산용."""
    day = _today_kst()
    n = 0
    for cur in ledger._fold().values():
        if cur.get("side") != "BUY":
            continue
        ts = cur.get("submitted_at") or 0
        if ts and datetime.datetime.fromtimestamp(
                ts, datetime.timezone(datetime.timedelta(hours=9))).date() == day:
            n += 1
    return n


def us_regular_open() -> bool:
    """I1 세션 게이트 — US 정규장만 True(dayMarket/pre/after 신규진입 hard-off)."""
    try:
        from bot import settings as cfg
        return bool(cfg.market_open("USD"))
    except Exception:
        return False              # 판정 불가 = 진입 금지(fail-closed)


def session_open_for(market: str) -> bool:
    """시장별 정규장 게이트(fail-closed). US=미 정규장, KR=한국 정규장.
    두 시장 모두 pre/after/dayMarket 신규진입 금지 — 정규 연속장만.
    US는 us_regular_open()에 위임(단일 소스)."""
    if market != "KR":
        return us_regular_open()
    try:
        from bot import settings as cfg
        return bool(cfg.market_open("KRW"))
    except Exception:
        return False


def check_new_entry(symbol: str, *, open_positions: int,
                    risk_pct: float, qty_is_whole: bool = True,
                    session_open: bool | None = None,
                    market: str = "US") -> tuple[bool, str]:
    """신규 진입 허용 판정. (ok, 사유). 모든 게이트 fail-closed.
    market: 'US'|'KR' — 세션 게이트를 해당 시장 정규장으로 라우팅."""
    p = profile()
    if session_open is None:
        session_open = session_open_for(market)
    if not session_open:
        mk = "한국" if market == "KR" else "US"
        return False, f"{mk} 정규장 아님(장외/시간외 신규진입 금지)"
    if not qty_is_whole:
        return False, "whole-share만 허용(소수점 금지)"
    al = allowed_symbols()
    if p["allowlist_required"] and not al:
        return False, "ALLOWED_SYMBOLS 미설정(allowlist 필수 Stage)"
    # optional Stage에서는 None과 빈 목록이 모두 "수동 fence 없음"이다. 비어
    # 있지 않은 목록만 운영자 긴급 축소로 해석한다.
    if al and symbol.upper() not in al:
        return False, f"{symbol} allowlist 밖"
    max_positions = p.get("max_positions")
    if max_positions is not None and open_positions >= max_positions:
        return False, f"동시 보유 한도({max_positions}) 도달"
    if new_entries_today() >= p["max_new_per_day"]:
        return False, f"하루 신규 한도({p['max_new_per_day']}) 도달"
    if risk_pct > p["risk_cap"] + 1e-12:
        return False, f"risk {risk_pct:.4f} > cap {p['risk_cap']:.4f}"
    return True, "ok"
