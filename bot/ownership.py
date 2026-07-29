"""IS2/IS5 — 계좌 격리: 심볼 배제(baseline denylist) + 비대칭 대사·동결.

별도 봇 계좌(IS1-A)가 정공법이지만, 그래도 이 방어는 유지한다(사용자가 봇 계좌를
앱에서 만질 가능성·외부 입출금 — Codex B6 잔여위험).

IS2 — 심볼 비중첩:
  · arming 전 capture_baseline(holdings)로 **사용자 기보유 심볼**을 고정 저장.
  · 그 심볼은 봇의 **영구 매수 denylist**(같은 종목 병합=소유 경계 소실 차단).
  · **fail-closed**: baseline 파일이 없으면(캡처 전) 전 종목 매수 거부.
  · baseline은 자동으로 **줄지 않는다** — 새 외부 심볼로 늘기만(403→빈값이
    denylist를 비워버리는 사고 차단). 캡처 입력이 조회 실패(None)면 거부.

IS5 — 비대칭 대사·동결:
  · reconcile_claims(): 봇 원장 claim ≤ 브로커 holdings 수량만 강제.
    초과 감지 = claim 하향 없이 **그 종목 동결(close-only)** + P0(설명불가 변화).
  · 동결은 파일 영속 — 재시작에도 유지, 해제는 operator ack.
"""
from __future__ import annotations

import json
import os
import tempfile
import time

_BASE_DEFAULT = os.path.join(tempfile.gettempdir(), "user_baseline.json")
# 동결 상태는 **영속 경로**(모듈 옆)로 — /tmp 기본이면 재부팅/청소 시 파일이 사라져
#   close-only 동결이 조용히 풀린다(감사 수정 #10, fail-open). SYMBOL_FREEZE_PATH로 override.
_FREEZE_DEFAULT = os.path.join(os.path.dirname(__file__), "symbol_freeze.json")


def _bpath() -> str:
    return os.environ.get("USER_BASELINE_PATH", _BASE_DEFAULT)


def _fpath() -> str:
    return os.environ.get("SYMBOL_FREEZE_PATH", _FREEZE_DEFAULT)


def _atomic_write(path: str, obj) -> bool:
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def _load(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ── IS2 baseline denylist ─────────────────────────────────────────
def capture_baseline(holdings_rows: list[dict] | None) -> bool:
    """사용자 기보유 심볼 캡처. **줄어들지 않게 병합** 저장. 실패/None 입력=거부.

    holdings_rows: inquire-balance output1 (rt_cd=0로 확인된 것만 넘길 것).
    빈 리스트([])는 유효(신규 깨끗한 계좌) — None(조회 실패)과 구분.
    """
    if holdings_rows is None:
        return False                              # fail-closed: 실패로 캡처 금지
    new_syms = {str(r.get("ovrs_pdno") or r.get("pdno") or "").upper()
                for r in holdings_rows}
    new_syms.discard("")
    prev = _load(_bpath()) or {"symbols": [], "ts": 0}
    merged = sorted(set(prev.get("symbols", [])) | new_syms)   # 늘기만
    return _atomic_write(_bpath(), {"symbols": merged, "ts": time.time()})


def baseline() -> set[str] | None:
    """저장된 baseline 심볼 집합. **파일 없으면 None(=arming 안 됨, 전 거부)**."""
    d = _load(_bpath())
    if d is None:
        return None
    return {s.upper() for s in d.get("symbols", [])}


def buy_denied(symbol: str) -> tuple[bool, str]:
    """이 심볼 매수 금지인가. baseline 미캡처=전 종목 거부(fail-closed)."""
    b = baseline()
    if b is None:
        return True, "baseline 미캡처 — arming 전(전 종목 매수 거부)"
    if symbol.upper() in b:
        return True, f"{symbol}=사용자 기보유(영구 denylist·병합 차단)"
    if is_frozen(symbol):
        return True, f"{symbol} 동결(close-only)"
    return False, "ok"


# ── IS5 비대칭 대사·동결 ──────────────────────────────────────────
def freeze(symbol: str, why: str) -> None:
    d = _load(_fpath()) or {}
    d[symbol.upper()] = {"why": why, "ts": time.time()}
    _atomic_write(_fpath(), d)
    try:
        from bot import notify
        notify.send(f"🧊 종목 동결(close-only) — {symbol}: {why}", critical=True)
    except Exception:
        pass


def unfreeze(symbol: str, *, ack: str) -> None:
    """동결 해제 — operator ack 필수."""
    if not (ack and ack.strip()):
        raise PermissionError("동결 해제에는 operator ack 필요")
    d = _load(_fpath()) or {}
    d.pop(symbol.upper(), None)
    _atomic_write(_fpath(), d)


def is_frozen(symbol: str) -> bool:
    d = _load(_fpath()) or {}
    return symbol.upper() in d


def frozen_state() -> dict:
    """현재 close-only 동결의 안전한 사본을 반환한다(읽기 전용 운영 진단용)."""
    value = _load(_fpath())
    if not isinstance(value, dict):
        return {}
    return {
        str(symbol).upper(): dict(detail)
        for symbol, detail in value.items()
        if str(symbol).strip() and isinstance(detail, dict)
    }


def reconcile_claims(claims: dict[str, int],
                     holdings_rows: list[dict] | None) -> list[dict]:
    """비대칭 대사: 봇 claim ≤ 브로커 보유 수량만 강제(상향 절대 금지).

    claims: {symbol: 봇 원장 보유 수량}. holdings_rows: 브로커 output1(None=실패).
    초과(claim > broker) = 설명불가 → **동결 + 보고**. 반환: 문제 목록.
    """
    if holdings_rows is None:
        return [{"symbol": "*", "issue": "holdings 조회 실패 — 대사 불가(보수 유지)"}]
    broker = {}
    for r in holdings_rows:
        sym = str(r.get("ovrs_pdno") or r.get("pdno") or "").upper()
        try:
            # 해외 ovrs_cblc_qty · 국내 hldg_qty · 일반 qty (감사 수정 #7: hldg_qty
            #   누락 시 모든 KR 보유가 broker=0으로 읽혀 claim>broker 오탐 → 오동결).
            broker[sym] = broker.get(sym, 0) + int(float(
                r.get("ovrs_cblc_qty") or r.get("hldg_qty") or r.get("qty") or 0))
        except (TypeError, ValueError):
            continue
    issues = []
    for sym, claim in claims.items():
        have = broker.get(sym.upper(), 0)
        if claim > have:
            freeze(sym, f"claim {claim} > broker {have} — 설명불가 수량(외부 개입?)")
            issues.append({"symbol": sym, "issue": "claim>broker",
                           "claim": claim, "broker": have})
    return issues


def sell_cap(symbol: str, claim_qty: int, ord_psbl_qty: int) -> int:
    """봇 매도 상한 = min(봇 claim, 브로커 매도가능) — 사용자 몫 매도 방지(IS4)."""
    return max(0, min(int(claim_qty), int(ord_psbl_qty)))
