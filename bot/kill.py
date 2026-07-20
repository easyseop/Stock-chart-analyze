"""I6 — kill-switch 레벨 0~4 (latch + operator ack).

레벨 정의(Codex I6 수정채택 그대로):
  L0 정상
  L1 신규 진입 금지                       (기존 KILL_NEW_ENTRIES와 동치)
  L2 모든 신규 의도 금지(진입+증량), 기존 주문 관리만
  L3 파수꾼 손절/보호만 유지 (매수 계열 전면 금지)
  L4 자동 주문 전면 중지 — 단 보호를 '취소'하지는 않는다(무방비 방치 금지 원칙).
     KIS 미국주는 서버측 스톱이 없으므로 L4 = 손절도 자동으론 안 나감 → 즉시 P0,
     사람이 수동 대응(그래서 L4는 사실상 '사고 조사 모드').

latch 규칙(리뷰 확정):
  · 레벨은 코드/운영자가 **올릴 수만** 있다(자동 하향 없음).
  · **내리려면 operator ack 필요**: lower(level, ack="누가/왜") — ack 문자열이 없으면 거부.
  · 모든 변경은 원장(JSONL)에 who/why/ts로 기록(감사 추적).

저장: 상태 파일(JSON, 원자쓰기) — 프로세스 재시작에도 레벨 유지(래치의 요체).
환경변수 `KILL_LEVEL`이 파일보다 **높으면** 그 값을 즉시 채택(긴급 수동 상향용).
"""
from __future__ import annotations

import json
import os
import tempfile
import time

# 래치는 **영속 경로**(모듈 옆)가 기본 — /tmp면 재부팅/청소 시 올려둔 kill 레벨이
#   사라져 매매가 조용히 재개된다(감사 수정 #8, fail-open). KILL_STATE_PATH로 override.
_DEFAULT = os.path.join(os.path.dirname(__file__), "kill_switch.json")
LOG_PATH = os.path.join(os.path.dirname(__file__), "kill_log.jsonl")

# 액션 → 허용 최대 레벨 (이 레벨 '이하'에서만 허용)
_ALLOW_MAX = {
    "buy_new": 0,        # 신규 진입: L0만
    "buy_add": 1,        # 증량: L0~L1 (L2부터 모든 신규 의도 금지)
    "manage": 2,         # 기존 주문 정정/취소 관리: L0~L2
    "protect_sell": 3,   # 파수꾼 손절/보호 매도: L0~L3 (L4만 중지)
}


def _path() -> str:
    return os.environ.get("KILL_STATE_PATH", _DEFAULT)


def _log(ev: dict) -> None:
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), **ev}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _read_file() -> int:
    p = _path()
    if not os.path.exists(p):
        return 0                         # 파일 없음 = kill 미설정(정상 L0)
    try:
        with open(p, encoding="utf-8") as f:
            return int(json.load(f).get("level", 0))
    except Exception:                    # 파일은 있는데 손상/못읽음 → fail-closed:
        _log({"ev": "read_error", "path": p, "assumed_level": 1})  # 신규진입 차단
        return 1                         # (감사 수정: 예전엔 0 반환 = 매매 재개, 위험)


def _write_file(lv: int, who: str, why: str) -> None:
    p = _path()
    tmp = f"{p}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"level": lv, "who": who, "why": why, "ts": time.time()}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def level() -> int:
    """현재 레벨 = max(상태 파일, 환경변수 KILL_LEVEL). 환경변수는 상향 전용."""
    file_lv = _read_file()
    try:
        env_lv = int(os.environ.get("KILL_LEVEL", "0"))
    except ValueError:
        env_lv = 0
    return max(0, min(4, max(file_lv, env_lv)))


def raise_level(lv: int, who: str, why: str) -> int:
    """레벨 상향(자동/수동 모두 허용). 하향 시도는 무시(래치)."""
    lv = max(0, min(4, int(lv)))
    cur = level()
    if lv <= cur:
        return cur
    _write_file(lv, who, why)
    _log({"ev": "raise", "from": cur, "to": lv, "who": who, "why": why})
    try:
        from bot import notify
        notify.send(f"🚨 kill-switch L{cur}→L{lv} — {why} ({who})", critical=True)
    except Exception:
        pass
    return lv


def lower_level(lv: int, *, ack: str) -> int:
    """레벨 하향 — **operator ack 필수**(빈 문자열 거부). 감사 기록 남김."""
    if not (ack and ack.strip()):
        raise PermissionError("kill-switch 하향에는 operator ack(누가/왜)가 필요")
    lv = max(0, min(4, int(lv)))
    cur = level()
    if lv >= cur:
        return cur
    if int(os.environ.get("KILL_LEVEL", "0") or 0) > lv:
        raise PermissionError("환경변수 KILL_LEVEL이 더 높음 — env부터 내려야 함")
    _write_file(lv, "operator", ack)
    _log({"ev": "lower", "from": cur, "to": lv, "who": "operator", "ack": ack})
    return lv


def allows(action: str) -> bool:
    """이 액션이 현재 레벨에서 허용되나. 미지 액션은 보수적으로 거부."""
    mx = _ALLOW_MAX.get(action)
    if mx is None:
        return False
    return level() <= mx
