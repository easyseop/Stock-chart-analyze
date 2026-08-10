"""KIS 청산(익절) 관리자 — 전략의 매도 규칙을 KIS 계좌에 직접 집행.

정합성 점검(2026-07-24) 반영: 파수꾼은 손절만 집행해 KIS가 '이익 실현 없는
반쪽 전략'으로 돌았다(웹 모의와 보유 종목이 달라 매도 미러링만으론 커버 불가).
→ 봇 보유 전 종목에 전략 A의 청산 프로파일을 R단위로 집행:
  ① +1R 도달: 절반 익절 + 손절선 본전(진입가) 래칫
  ② 이후 트레일: 손절선 = max(현재, 최고가 − 1.5R)  (원전략 3×ATR ≈ 1.5R 근사)
  ③ 21일 타임스탑: +1R 미도달이면 전량 정리
  실제 '팔기'는 손절선 터치 시 파수꾼이 집행(래칫된 선) — 이 모듈은 선을 올리고
  절반익절·타임스탑만 직접 주문한다. 손절 로직과 충돌 없음.

호출: 파수꾼 check_once 말미(20초 주기·장중만). 실패는 사이클에 무해.
상태: bot/kis_exits_state.json {code: {half, high}} — 포지션 소멸 시 자동 정리.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import tempfile
import time

from bot import kis_positions, ledger, notify, settings, stall_exit

# 같은 키 매도 실패 경보의 중복 억제(2026-08-11 TAP 타임스탑 1분 폭주 실측).
#   고정 키(time/btgt)가 원장에 rejected로 종결돼 있으면 재시도가 멱등 게이트에서
#   거부되는 무해한 no-op인데, 사이클마다 critical 경보가 반복돼 진짜 경보가
#   묻혔다. 실패 경보는 키당 1시간 1회로 억제하고 접수 성공은 항상 알린다.
#   (재시도 가능한 키 설계는 CODEX_SPEC_SELL_REJECT_RECONCILE R5에서 근본 수정.)
_SELL_FAIL_NOTIFIED: dict[str, float] = {}
_SELL_FAIL_REALERT_S = 3600.0


def _sell_fail_alert_due(key: str, now: float) -> bool:
    """이 키의 실패 경보를 지금 보내야 하나(억제 창 밖) — 보낸다고 표시까지."""
    last = _SELL_FAIL_NOTIFIED.get(key)
    if last is not None and now - last < _SELL_FAIL_REALERT_S:
        return False
    _SELL_FAIL_NOTIFIED[key] = now
    return True

STATE_PATH = os.environ.get(
    "KIS_EXITS_STATE", os.path.join(os.path.dirname(__file__), "kis_exits_state.json"))
TRAIL_R = 1.5           # off/shadow에서도 기존 전략값은 환경변수로 바꾸지 않는다.
TIME_STOP_DAYS = 21      # autopaper.TIME_STOP_DAYS와 동일
STALL_MODE = settings.STALL_EXIT_MODE
STALL_TIGHTEN_DAYS = settings.STALL_TIGHTEN_DAYS
STALL_EXIT_DAYS = settings.STALL_EXIT_DAYS
STALL_NEW_HIGH_R = settings.STALL_NEW_HIGH_R
STALL_TIGHT_TRAIL_R = settings.STALL_TIGHT_TRAIL_R


# 정체 상태에서 숫자/불리언으로 소비되는 필드 — 타입이 틀리면 "유효한 JSON이지만
#   손상된 상태"다. 알 수 없는 키는 미래 확장을 위해 검사하지 않는다.
_STATE_BOOL_FIELDS = (
    "half", "stall_tight_notified", "stall_exit_notified",
    "half_stop_raised", "state_recovery_quarantine",
)
_STATE_NUM_FIELDS = ("high", "stall_anchor_high", "stall_days",
                     "half_target", "half_filled")
_STATE_STR_FIELDS = ("high_day", "counted_days")


def _valid_state_shape(payload: dict) -> bool:
    for code, xs in payload.items():
        if not isinstance(code, str) or not isinstance(xs, dict):
            return False
        for key in _STATE_BOOL_FIELDS:
            if key in xs and not isinstance(xs[key], bool):
                return False
        for key in _STATE_NUM_FIELDS:
            if key in xs and (isinstance(xs[key], bool)
                              or not isinstance(xs[key], (int, float))):
                return False
        for key in _STATE_STR_FIELDS:
            if key in xs and not isinstance(xs[key], str):
                return False
        if "half_keys" in xs and not (
                isinstance(xs["half_keys"], list)
                and all(isinstance(k, str) for k in xs["half_keys"])):
            return False
    return True


def _load_with_status() -> tuple[dict, bool]:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        return {}, False
    except Exception:
        return {}, True
    # dict이기만 하면 통과시키던 구멍: {"AAPL": "…"} 같은 스키마 손상이 격리를
    #   우회해 manage에서 예외/오판을 냈다. 형태가 틀리면 기존 손상 경로로 보낸다.
    if not isinstance(payload, dict) or not _valid_state_shape(payload):
        return {}, True
    return payload, False


def _load() -> dict:
    return _load_with_status()[0]


def _save(st: dict) -> None:
    parent = os.path.dirname(os.path.abspath(STATE_PATH)) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".kis-exits-", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as f:
            fd = -1
            json.dump(st, f, ensure_ascii=False, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_PATH)
        os.chmod(STATE_PATH, 0o600)
        dfd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _alert_corrupt_state_once() -> None:
    """동일 손상 바이트에는 한 번만 경보하고 forensic 사본을 남긴다."""
    try:
        with open(STATE_PATH, "rb") as fp:
            raw = fp.read()
    except OSError:
        raw = b"unreadable"
    digest = hashlib.sha256(raw).hexdigest()
    marker = STATE_PATH + ".corrupt-alert"
    try:
        with open(marker, encoding="ascii") as fp:
            if fp.read().strip() == digest:
                return
    except OSError:
        pass
    archive = STATE_PATH + f".corrupt-{digest[:12]}"
    if raw != b"unreadable" and not os.path.exists(archive):
        try:
            shutil.copy2(STATE_PATH, archive)
            os.chmod(archive, 0o600)
        except OSError:
            pass
    notify.send(
        "🚨 <b>정체청산 상태 손상 — 수동 확인</b> · 즉시 매도하지 않고 "
        "기존 1.5R 추적 보호를 유지하며 정체일을 0부터 다시 셉니다.",
        critical=True, category="trade")
    try:
        with open(marker + ".tmp", "w", encoding="ascii") as fp:
            fp.write(digest + "\n")
        os.replace(marker + ".tmp", marker)
        os.chmod(marker, 0o600)
    except OSError:
        pass


def decide(entry: float, stop0: float, cur_stop: float, price: float,
           qty: int, xs: dict, opened: str, today: str,
           stall_mode: str = "off") -> list[tuple]:
    """청산 판단(순수 함수) — 영구 상태(half)는 바꾸지 않는다(코덱스 P0 반영:
    주문 성공 전 half=True 저장 시 매도 실패가 영구 누락되던 버그). 반환 액션:
      ("half_sell", 수량)  — +1R 절반 매도 제안(성공 시 호출부가 half 확정)
      ("half_done",)       — 1주라 매도 없이 래칫만(호출부가 half 확정)
      ("raise", 값)        — 손절선 상향
      ("sell", 수량, 사유) — 전량 정리(타임스탑)
    xs는 호출부가 만든 스냅샷이며 이 함수는 수정하지 않는다.
    """
    acts: list[tuple] = []
    if entry <= 0 or stop0 <= 0 or price <= 0 or qty <= 0 or entry <= stop0:
        return acts
    R = entry - stop0
    high = max(float(xs.get("high") or 0.0), price)

    # ① +1R 절반 익절 제안 — half 확정은 주문 성공 후 호출부가
    state_trusted = not xs.get("state_recovery_quarantine")
    if state_trusted and not xs.get("half") and price >= entry + R:
        if qty >= 2:
            acts.append(("half_sell", qty // 2))
        else:
            acts.append(("half_done",))
    # ② 트레일(절반익절 확정 후) — 최고가 − 1.5R, 올리기만
    if xs.get("half"):
        width = stall_exit.trail_r(
            xs, run_mode=stall_mode, base_r=TRAIL_R,
            tight_r=STALL_TIGHT_TRAIL_R,
            tighten_days=STALL_TIGHTEN_DAYS)
        trail = high - width * R
        if trail > cur_stop:
            acts.append(("raise", trail))
        if stall_exit.exit_due(
                xs, run_mode=stall_mode, exit_days=STALL_EXIT_DAYS):
            acts.append(("sell", qty, f"정체 청산({STALL_EXIT_DAYS}거래일)"))
    # ③ 타임스탑 — 21일 지나도 +1R 미도달이면 전량 정리
    try:
        d0 = datetime.date.fromisoformat(str(opened))
        dn = datetime.date.fromisoformat(str(today))
        if state_trusted and not xs.get("half") \
                and (dn - d0).days >= TIME_STOP_DAYS \
                and price < entry + R:
            acts.append(("sell", qty, f"타임스탑 {TIME_STOP_DAYS}일"))
    except (ValueError, TypeError):
        pass                                   # 개시일 불명 — 타임스탑 생략
    return acts


def decide_b(target: float, price: float, qty: int,
             opened: str, today: str) -> list[tuple]:
    """슬리브 B 전용 청산(코덱스 P1 반영) — A 규칙(+1R/트레일) 대신:
      · 목표(VAH) 도달 → 전량 익절   · 타임스탑 21일 → 전량 정리
    손절(반등저점 아래)은 파수꾼이 기존 경로로 집행."""
    acts: list[tuple] = []
    if qty <= 0 or price <= 0:
        return acts
    if target and price >= target:
        acts.append(("sell", qty, "B 목표(VAH) 도달"))
        return acts
    try:
        d0 = datetime.date.fromisoformat(str(opened))
        dn = datetime.date.fromisoformat(str(today))
        if (dn - d0).days >= TIME_STOP_DAYS:
            acts.append(("sell", qty, f"B 타임스탑 {TIME_STOP_DAYS}일"))
    except (ValueError, TypeError):
        pass
    return acts


def _half_base(code: str, rec: dict) -> str:
    return f"xe:{code}:half:{rec.get('opened', '')}"


def _half_marker_id(code: str, rec: dict) -> str:
    identity = str(rec.get("pos_key") or "").strip()
    if not identity:
        identity = f"{code}:{rec.get('opened', '')}"
    return f"half:{identity}"


def _half_progress(code: str, rec: dict, xs: dict) -> dict:
    """절반익절 주문들의 브로커 확인 체결만 합산한다.

    ACK(접수)는 0주 체결이다. 부분체결 주문이 끝나면 확인된 수량을 보존하고 잔여만
    새 키로 재시도한다. 상태 파일이 주문 직후 쓰이기 전에 프로세스가 죽어도 원장 키
    접두사로 주문을 복구할 수 있다.
    """
    base = _half_base(code, rec)
    orders = ledger.orders_for(code, side="SELL", key_prefix=base)
    target = int(xs.get("half_target") or 0)
    if target <= 0 and orders:
        target = int(orders[0].get("intended") or 0)
    filled = sum(max(0, int(o.get("filled") or 0)) for o in orders)
    open_keys = {o["key"] for o in ledger.open_orders(code, side="SELL")}
    pending = any(o["key"] in open_keys for o in orders)
    xs["half_target"] = target
    xs["half_filled"] = min(target, filled) if target > 0 else filled
    xs["half_keys"] = [o["key"] for o in orders]
    return {"base": base, "target": target, "filled": filled,
            "residual": max(0, target - filled), "pending": pending,
            "attempts": len(orders)}


def _capture_order_result(key: str, code: str, qty: int, reason: str,
                          result) -> tuple[str, int]:
    """테스트/레거시 브로커의 즉시 결과도 원장 상태로 정규화한다.

    KIS 어댑터는 kis_orders 안에서 이미 선기록한다. 없는 경우에만 여기서 선기록해
    절반익절의 확인 상태가 프로세스 메모리에만 남지 않게 한다.
    """
    if isinstance(result, dict):
        state = str(result.get("state") or "rejected")
        filled = max(0, int(result.get("filled") or 0))
        open_order = result.get("open")
        if open_order is None and state == "partial":
            open_order = False                    # 즉시 partial 보고는 그 응답 기준 종료
    else:
        state, filled = (("filled", int(qty)) if result else ("rejected", 0))
        open_order = False
    cur = ledger.state_of(key)
    if cur is None:
        ledger.record_submit(key, code, int(qty), reason,
                             meta={"side": "SELL"})
        cur = ledger.state_of(key)
    if (str((cur or {}).get("state") or "") != state
            or int((cur or {}).get("filled") or 0) != filled):
        ledger.on_result(
            key, state, filled,
            open_order=(False if state in ("filled", "rejected")
                        else open_order))
    return state, filled


def _confirm_half_if_complete(code: str, rec: dict, xs: dict,
                              entry: float, cur_stop: float) -> bool:
    """목표 수량의 체결이 원장에서 확인된 뒤에만 half·본전 손절을 확정."""
    durable_marker = rec.get("half_done") is True
    legacy_one_share_proof = (
        int(rec.get("qty") or 0) == 1 and entry > 0 and cur_stop >= entry)
    if durable_marker or legacy_one_share_proof:
        xs["half"] = True
        xs.pop("state_recovery_quarantine", None)
        if legacy_one_share_proof and not durable_marker:
            kis_positions.mark_half_done(
                code, event_id=_half_marker_id(code, rec))
        # mark_half_done과 raise_stop 사이 크래시로 본전 래칫이 유실된 창을
        # 멱등 복구한다. 원장 stop 기준으로만 재적용해 중복 이벤트를 막는다.
        if entry > 0 and float(rec.get("stop") or 0) < entry:
            kis_positions.raise_stop(code, entry)
            notify.send(
                f"🔒 <b>손절선 본전 복구</b> — {code} half 확정 상태인데 "
                f"본전 래칫이 원장에 없어 재적용", critical=True)
        xs["half_stop_raised"] = True
        return True
    progress = _half_progress(code, rec, xs)
    if progress["target"] <= 0 or progress["filled"] < progress["target"]:
        return False
    xs["half"] = True
    xs.pop("state_recovery_quarantine", None)
    kis_positions.mark_half_done(
        code, event_id=_half_marker_id(code, rec))
    if not xs.get("half_stop_raised"):
        if entry > cur_stop:
            kis_positions.raise_stop(code, entry)
        xs["half_stop_raised"] = True
        notify.send(
            f"✅ <b>KIS 익절 +1R 절반 체결 확정</b> — {code} "
            f"{progress['target']}주 · 손절선 본전으로",
            critical=True)
    return True


def manage(broker, held: dict, today: str) -> None:
    """봇 보유(진입 기록 있는 종목)에 청산 규칙 적용. 파수꾼이 매 사이클 호출."""
    kpos = kis_positions.load()
    st, corrupt_state = _load_with_status()
    changed = corrupt_state
    if corrupt_state:
        _alert_corrupt_state_once()
        # 손상 감지 시 현재 열린 시장 종목만 표시하면, 닫혀 있던 다른 시장 종목은
        # 다음 개장 때 정상 신규상태로 오인돼 즉시 재익절/타임스탑될 수 있다.
        # 시장과 무관한 포지션 원장 전체를 먼저 격리하고 증명된 half만 개별 해제한다.
        for code in kpos:
            st.setdefault(code, {"half": False, "high": 0.0})[
                "state_recovery_quarantine"] = True
    for code, p in list(held.items()):
        rec = kpos.get(code)
        if not rec or not rec.get("entry"):
            continue                            # 봇 진입 기록 없음(기보유 등) — 제외
        xs = st.setdefault(code, {"half": False, "high": 0.0})
        before = json.dumps(xs, sort_keys=True)
        entry = float(rec["entry"])
        stop0 = float(rec.get("stop0") or rec.get("stop") or 0)
        cur_stop = float(p.get("stop") or rec.get("stop") or 0)
        qty = int(p.get("q") or 0)

        # 이전 사이클 ACK/부분체결을 먼저 확정한다. 접수만으로 half=True가 되지 않는다.
        if _confirm_half_if_complete(code, rec, xs, entry, cur_stop):
            cur_stop = max(cur_stop, entry)
        if json.dumps(xs, sort_keys=True) != before:
            changed = True
        # 살아있는 SELL/취소만 새 청산을 전면 막는다. BUY 잔량 때문에 손절선
        # 래칫까지 멈추면 보호가 후퇴한다. 다만 실제 매도 액션은 아래에서 BUY
        # 취소확인 뒤에만 허용해 추가체결과의 초과매도 경합을 막는다.
        if (ledger.open_order_count(code, side="SELL") >= 1
                or ledger.open_order_count(code, side="CANCEL") >= 1):
            st[code] = xs
            continue
        price = broker.quote(code, p.get("ccy", "USD"))
        if not price or price <= 0:
            st[code] = xs
            continue
        if rec.get("sleeve") == "B":               # B 전용 청산(목표 VAH·타임스탑)
            acts = decide_b(float(rec.get("target") or 0), price, qty,
                            rec.get("opened", ""), today)
        else:
            next_xs, transitions = stall_exit.advance(
                xs, half_confirmed=bool(xs.get("half")), price=price,
                entry=entry, stop0=stop0, day=today,
                valid_trading_day=settings.market_open(
                    str(p.get("ccy") or rec.get("ccy") or "USD")),
                enabled=stall_exit.mode(STALL_MODE) != "off",
                new_high_r=STALL_NEW_HIGH_R,
                tighten_days=STALL_TIGHTEN_DAYS,
                exit_days=STALL_EXIT_DAYS)
            xs.clear()
            xs.update(next_xs)
            if "tighten" in transitions and not xs.get("stall_tight_notified"):
                xs["stall_tight_notified"] = True
                proposal = max(
                    cur_stop,
                    float(xs.get("high") or price)
                    - STALL_TIGHT_TRAIL_R * (entry - stop0))
                notify.send(
                    f"🔒 <b>정체 {STALL_TIGHTEN_DAYS}거래일</b> — {code} "
                    f"추적폭 {TRAIL_R:.1f}R→{STALL_TIGHT_TRAIL_R:.1f}R "
                    f"{'그림자 제안' if STALL_MODE == 'shadow' else '적용'} "
                    f"(손절선 {proposal:.2f}, 내림 없음)",
                    category="trade")
            if (STALL_MODE == "shadow" and "exit" in transitions
                    and not xs.get("stall_exit_notified")):
                xs["stall_exit_notified"] = True
                notify.send(
                    f"🌓 <b>정체 {STALL_EXIT_DAYS}거래일 그림자 청산 제안</b> — "
                    f"{code} 잔량 {qty}주 · 실제 주문 0건",
                    category="trade")
            acts = decide(entry, stop0, cur_stop, price, qty, xs,
                          rec.get("opened", ""), today, STALL_MODE)
        # 보호선 상향은 신규 BUY 취소 대기와 무관하게 먼저 적용한다. 정체청산
        # 매도가 일시 실패/보류돼도 잔량 보호가 이전 선에 멈추지 않는다.
        for act in [row for row in acts if row[0] == "raise"]:
            changed = True
            kis_positions.raise_stop(code, float(act[1]))
            cur_stop = max(cur_stop, float(act[1]))
            notify.send(f"🔒 <b>손절선 상향</b> — {code} → {act[1]:.2f} "
                        f"(이익 보호 래칫)")
        acts = [row for row in acts if row[0] != "raise"]
        has_open_buy = ledger.open_order_count(code, side="BUY") >= 1
        wants_sell = any(act[0] in ("half_sell", "sell") for act in acts)
        if has_open_buy and wants_sell:
            # 실제 KIS 경로는 미체결 BUY를 취소 접수만으로 믿지 않고 브로커
            # 미체결 목록에서 소멸할 때까지 확인한다. 테스트/레거시 브로커는
            # 그 대사를 증명할 수 없으므로 매도를 보수적으로 미룬다.
            if str(getattr(broker, "name", "")).lower().startswith("kis"):
                from bot import kis_pending
                if not kis_pending.cancel_open_buys_for_protection(code):
                    st[code] = xs
                    changed = changed or json.dumps(xs, sort_keys=True) != before
                    continue
            else:
                st[code] = xs
                changed = changed or json.dumps(xs, sort_keys=True) != before
                continue
        for act in acts:
            changed = True
            if act[0] == "half_done":              # 1주 — 매도 없이 본전 래칫만
                xs["half"] = True
                xs["half_stop_raised"] = True
                xs.pop("state_recovery_quarantine", None)
                kis_positions.mark_half_done(
                    code, event_id=_half_marker_id(code, rec))
                if entry > cur_stop:
                    kis_positions.raise_stop(code, entry)
                    notify.send(f"🔒 <b>손절선 본전</b> — {code} (+1R 도달, 1주라 홀드)")
            elif act[0] == "half_sell":
                progress = _half_progress(code, rec, xs)
                if progress["target"] <= 0:
                    xs["half_target"] = int(act[1])
                    progress = _half_progress(code, rec, xs)
                sq = int(progress["residual"])
                if sq <= 0:
                    _confirm_half_if_complete(code, rec, xs, entry, cur_stop)
                    continue
                key = f"{progress['base']}#{progress['attempts'] + 1}"
                r = broker.place_sell(code, sq, "익절 +1R 절반", key)
                rstate, filled = _capture_order_result(
                    key, code, sq, "익절 +1R 절반", r)
                confirmed = _confirm_half_if_complete(
                    code, rec, xs, entry, cur_stop)
                if rstate == "ack":
                    status = "접수 · 체결 확인 대기(손절선 유지)"
                elif rstate == "partial" and filled > 0:
                    status = f"부분체결 {filled}주 · 잔여는 확인 후 재시도"
                elif confirmed:
                    status = "체결 확정 · 손절선 본전으로"
                else:
                    status = "실패/미확정 — 다음 사이클 대사·재시도"
                notify.send(f"💰 <b>KIS 익절 +1R 절반</b> — {code} {sq}주 매도 "
                            f"{status}",
                            critical=True)
            elif act[0] == "sell":
                _, sq, why = act
                if "정체 청산" in why:
                    base = f"xe:{code}:stall:{rec.get('opened','')}"
                    key = f"{base}#{ledger.attempts(base) + 1}"
                else:
                    key = f"xe:{code}:{'btgt' if '목표' in why else 'time'}:{rec.get('opened','')}"
                r = broker.place_sell(code, int(sq), why, key)
                ok = bool(r) if not isinstance(r, dict) else r.get("state") in ("ack", "filled")
                if ok:
                    _SELL_FAIL_NOTIFIED.pop(key, None)   # 성공 → 억제 리셋
                    notify.send(f"💰 <b>KIS {why}</b> — {code} {sq}주 매도 접수",
                                critical=True, category="trade")
                elif _sell_fail_alert_due(key, time.time()):
                    notify.send(f"💰 <b>KIS {why}</b> — {code} {sq}주 매도 실패 "
                                "— 재시도는 계속, 같은 실패 경보는 1시간 억제",
                                critical=True, category="trade")
        st[code] = xs
    # 시장별 holdings()는 열린 시장만 반환한다. KR 장중에 US 상태를 지우지 않도록
    # 소멸 판정은 시장과 무관한 봇 포지션 원장을 단일 기준으로 쓴다.
    for code in list(st):
        if code not in kpos:
            st.pop(code, None)
            changed = True
    if changed or st:
        _save(st)
