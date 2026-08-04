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


def _log_path() -> str:
    """감사 로그 경로 — 상태 경로를 옮기면 로그도 함께 옮긴다.

    종전에는 모듈 상수 하나만 써서, KILL_STATE_PATH로 상태를 격리한 테스트도
    **운영 감사로그**에 기록을 남겼다(`who=test·why=readiness` 오염 실측).
    사고 조사 때 실제 상승 사유와 테스트 흔적이 섞여 판단을 흐린다.
    """
    override = os.environ.get("KILL_LOG_PATH")
    if override:
        return override
    state = _path()
    if os.path.abspath(state) == os.path.abspath(_DEFAULT):
        return LOG_PATH
    return os.path.join(
        os.path.dirname(os.path.abspath(state)) or ".", "kill_log.jsonl")


def _log(ev: dict) -> None:
    try:
        with open(_log_path(), "a", encoding="utf-8") as f:
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


def _write_file(lv: int, who: str, why: str) -> bool:
    p = _path()
    tmp = f"{p}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"level": lv, "who": who, "why": why, "ts": time.time()}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        return True
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        _log({"ev": "write_error", "path": p, "requested_level": lv,
              "who": who, "why": why})
        return False


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
    if not _write_file(lv, who, why):
        return cur                              # 성공처럼 목표 레벨을 반환하지 않음
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
    if not _write_file(lv, "operator", ack):
        return cur
    _log({"ev": "lower", "from": cur, "to": lv, "who": "operator", "ack": ack})
    return lv


def allows(action: str) -> bool:
    """이 액션이 현재 레벨에서 허용되나. 미지 액션은 보수적으로 거부."""
    mx = _ALLOW_MAX.get(action)
    if mx is None:
        return False
    return level() <= mx


def main(argv: list[str] | None = None) -> int:
    """운영자가 문서의 `python -m bot.kill LEVEL 사유`를 그대로 쓸 수 있는 CLI."""
    import argparse
    ap = argparse.ArgumentParser(description="매매봇 kill-switch 조회·변경")
    ap.add_argument("level", nargs="?", type=int, help="목표 레벨(0~4)")
    ap.add_argument("reason", nargs="*", help="변경 사유(감사 로그에 기록)")
    ap.add_argument("--who", default=os.environ.get("USER", "operator"),
                    help="상향 수행자")
    ap.add_argument("--lower", action="store_true",
                    help="레벨 하향(operator ack로 사유 필수)")
    # argparse는 가변 위치인자(reason) 사이에 낀 옵션을 못 읽는다. 사고 대응 중에
    #   `kill 0 --lower "사유"`처럼 치면 통째로 거부돼 하향이 막혔다(실측). 남은
    #   토막은 사유로 합쳐 순서에 관계없이 동작시키되, 모르는 **옵션**은 계속 거부.
    args, extra = ap.parse_known_args(argv)
    unknown_flags = [a for a in extra if a.startswith("-")]
    if unknown_flags:
        ap.error(f"알 수 없는 옵션: {' '.join(unknown_flags)}")
    if args.level is None:
        print(f"L{level()}")
        return 0
    why = " ".join(list(args.reason) + extra).strip()
    if not why:
        ap.error("레벨 변경 사유가 필요합니다")
    if args.lower:
        out = lower_level(args.level, ack=why)
    else:
        out = raise_level(args.level, args.who, why)
    print(f"L{out}")
    reached = (out == max(0, min(4, args.level)) if args.lower
               else out >= max(0, min(4, args.level)))
    return 0 if reached else 2


if __name__ == "__main__":
    raise SystemExit(main())
