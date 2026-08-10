"""서버 자가진단 스냅샷 발행(읽기 전용) — SSH 없는 원격 진단 루프.

사용자 요청(2026-08-05): 'KIS 잔고 조회 실패' 같은 경보가 왔을 때 SSH 가능한
컴퓨터가 없어도 서버 상태를 확인·전달받을 방법이 필요하다. 텔레그램 `/진단`은
사람이 당기는 수동 루프, 이 모듈은 **주기 발행 자동 루프**다: 텔레그램 봇
프로세스가 주기적으로 `publish()`를 호출해 상태 스냅샷을 ntfy 토픽에 올리고,
원격(Claude 세션 등)은 `https://ntfy.sh/<topic>/json?poll=1`로 읽는다.

원칙:
  · **읽기 전용** — 주문·kill 변경·상태 변이가 전혀 없다(조회·파일 읽기만).
  · **시크릿 0** — kill 레벨·heartbeat 나이·시장별 조회 ok/fail·원장 카운트·
    서비스 active 여부·안전 플래그만. 계좌번호·금액·토큰·심볼 목록은 담지 않는다.
  · 발행 실패는 무해(다음 주기 재시도) — 매매 경로에 영향을 주지 않는다.

실행(수동 1회): python -m bot.ops_status
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import time
import urllib.request

_US_EXCGS = ("NASD", "NYSE", "AMEX")


def snapshot() -> dict:
    """현재 서버 상태의 무시크릿 스냅샷. 각 항목은 실패해도 나머지를 계속한다."""
    out: dict = {
        "generated_at": datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=9))).isoformat(),
        "v": 1,
    }
    try:
        from bot import kill
        out["kill_level"] = kill.level()
    except Exception as e:
        out["kill_level_error"] = type(e).__name__
    try:
        from bot import heartbeat
        age = heartbeat.age_s()
        out["heartbeat_age_s"] = None if age is None else round(age, 1)
    except Exception as e:
        out["heartbeat_error"] = type(e).__name__
    try:
        from bot import kis
        markets = {}
        for market, excg in [("KR", None)] + [("US", e) for e in _US_EXCGS]:
            r = (kis.positions_detail(market) if market == "KR"
                 else kis.positions_detail(market, excg=excg))
            markets[market if market == "KR" else excg] = (
                None if r is None else len(r))       # None=조회 실패, 숫자=보유 수
        out["kis_positions_query"] = markets
        out["kis_query_ok"] = all(v is not None for v in markets.values())
    except Exception as e:
        out["kis_query_error"] = type(e).__name__
    try:
        from bot import ledger
        fold = ledger._fold()
        out["open_orders"] = sum(1 for c in fold.values() if c.get("open_order"))
        out["unknown_orders"] = sum(
            1 for c in fold.values() if c.get("state") == "unknown")
    except Exception as e:
        out["ledger_error"] = type(e).__name__
    try:
        from bot import sentinel
        _rows, feed_age = sentinel._fetch_positions()
        out["positions_feed_age_min"] = (
            None if feed_age is None else round(float(feed_age), 1))
    except Exception as e:
        out["feed_error"] = type(e).__name__
    services = {}
    for unit in ("sentinel", "buyloop", "watchdog", "telegram", "portfolio-web"):
        try:
            services[unit] = subprocess.run(
                ["systemctl", "is-active", unit], capture_output=True,
                text=True, timeout=5).stdout.strip() or "?"
        except Exception:
            services[unit] = "?"
    out["services"] = services
    env = os.environ
    out["flags"] = {
        "kis_env": env.get("KIS_ENV"),
        "stage": env.get("TRADE_STAGE"),
        "allow_buy": env.get("ALLOW_BUY") == "1",
        "orders_enabled": env.get("KIS_ORDERS_ENABLED") == "1",
        "fallback": env.get("ORACLE_SIGNAL_FALLBACK_ENABLED", "0"),
    }
    return out


def publish(snap: dict | None = None) -> bool:
    """스냅샷을 ntfy 토픽에 발행. 실패는 False(무해 — 다음 주기 재시도)."""
    from bot import settings
    try:
        body = json.dumps(snap if snap is not None else snapshot(),
                          ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(
            "https://ntfy.sh/" + settings.OPS_STATUS_TOPIC, data=body,
            method="POST", headers={"Title": "ops-status", "Priority": "min",
                                    "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10):
            pass
        return True
    except Exception:
        return False


# 텔레그램 봇 루프가 호출하는 주기 발행기(프로세스 내 상태만 사용).
_last_publish = 0.0
PUBLISH_INTERVAL_S = int(
    os.environ.get("OPS_STATUS_INTERVAL_S", "600") or 600)   # 기본 10분


def maybe_publish() -> bool:
    """마지막 발행 후 간격이 지났으면 발행. 호출 비용이 없도록 시간부터 확인."""
    global _last_publish
    now = time.time()
    if now - _last_publish < max(60, PUBLISH_INTERVAL_S):    # 최소 1분 하한
        return False
    _last_publish = now                    # 실패해도 갱신 — 실패 폭주 방지
    return publish()


# ── kill L1+ 지속 리마인드(사용자 요청 2026-08-10) ─────────────────────────
# 8/7 밤 워치독 자동 상향이 주말 내내 L1로 남아 월요일 매수가 통째로 빠진 사건
#   재발 방지: 상향 순간의 즉시 경보(kill 모듈이 발송)와 별개로, L1 이상이
#   '유지되는 동안' 주기적으로 알려 잊히지 않게 한다. L0 복구도 1회 알린다.
KILL_REMIND_INTERVAL_S = int(
    os.environ.get("KILL_REMIND_INTERVAL_S", "14400") or 14400)   # 기본 4시간
_kill_remind_at = 0.0        # 다음 리마인드 허용 시각
_kill_last_level = None      # 마지막 관찰 레벨(전환 감지·재시작 시 None)


def maybe_remind_kill(now: float | None = None) -> bool:
    """kill 레벨을 관찰해 L1+ 지속 리마인드·L0 복구 알림을 보낸다(실패 무해).

    · 상향 직후는 kill 모듈의 즉시 경보가 있으므로 첫 리마인드는 한 주기 뒤.
    · 프로세스 재시작 직후도 즉시 울리지 않는다(재시작 스팸 방지, prev=None).
    · critical=True로 보내 NOTIFY_MODE=trade_only에서도 걸러지지 않는다.
    """
    global _kill_remind_at, _kill_last_level
    try:
        from bot import kill, notify
        t = time.time() if now is None else float(now)
        lv = kill.level()
        prev = _kill_last_level
        _kill_last_level = lv
        if lv <= 0:
            _kill_remind_at = 0.0
            if prev is not None and prev >= 1:
                notify.send("✅ kill-switch L0 복구 관찰 — 신규매수 재개",
                            critical=True)
                return True
            return False
        if prev is None or prev <= 0:      # 방금 상향/재시작 — 한 주기 뒤부터
            _kill_remind_at = t + max(600, KILL_REMIND_INTERVAL_S)
            return False
        if t < _kill_remind_at:
            return False
        _kill_remind_at = t + max(600, KILL_REMIND_INTERVAL_S)
        st = kill.status()
        since_h = (t - st["ts"]) / 3600 if st.get("ts") else None
        dur = f" {since_h:.1f}시간째" if since_h and 0 < since_h < 24 * 30 else ""
        why = f" · 사유: {st['why']}" if st.get("why") else ""
        notify.send(
            f"⏳ kill-switch L{lv} 유지 중{dur} — 신규매수 차단, "
            f"매도 보호는 정상{why}\n"
            "해제하려면: 서버 readiness(--scope l0) GO 확인 후 kill 하향",
            critical=True)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    print(json.dumps(snapshot(), ensure_ascii=False, indent=1))
