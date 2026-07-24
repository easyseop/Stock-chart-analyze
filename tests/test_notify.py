"""P0 알림 이중화(ntfy) 검증 — 텔레그램 장애에도 손절 경보 도달(RELIABILITY B4).

  1) critical=False → ntfy 전면 미호출(일상 알림은 텔레그램만, ntfy 폭주 방지)
  2) critical=True + NTFY_TOPIC 미설정 → ntfy 네트워크 0(동작은 기존과 동일)
  3) critical=True + NTFY_TOPIC 설정 → ntfy POST 발행(텔레그램과 독립)
  4) 한글·이모지 본문 안전 — HTTP 헤더(latin-1)로 안 새고 본문은 UTF-8
  5) ntfy 실패해도 send()는 예외 없이 반환(빌드 안 죽임)

실행: python -m tests.test_notify
"""
from __future__ import annotations

import io
import os
import sys
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _clean_env():
    for k in ("NTFY_TOPIC", "NTFY_SERVER", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
              "NOTIFY_MODE"):
        os.environ.pop(k, None)


def test_noncritical_never_calls_ntfy():
    _clean_env()
    os.environ["NTFY_TOPIC"] = "t"          # 설정돼 있어도
    from bot import notify
    with mock.patch.object(notify, "_ntfy") as m:
        notify.send("일상 매수 제안")          # critical 생략 → 기본 False
    assert m.call_count == 0
    print("[PASS] critical=False → ntfy 미호출(설정돼 있어도)")


def test_critical_unset_topic_no_network():
    _clean_env()                            # NTFY_TOPIC 없음
    from bot import notify
    calls = []
    with mock.patch("urllib.request.urlopen", lambda *a, **k: calls.append(a) or _Resp()):
        notify.send("🔴 손절 경보", critical=True)
    # 텔레그램도 미설정(드라이런)이라 urlopen 자체가 0회여야 함
    assert calls == [], "NTFY_TOPIC 미설정인데 네트워크 발생"
    print("[PASS] critical=True + 토픽 미설정 → 네트워크 0(기존 동작 유지)")


def test_critical_with_topic_posts_ntfy():
    _clean_env()
    os.environ["NTFY_TOPIC"] = "stockbot-p0-xyz"
    from bot import notify
    seen = {}

    def fake(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["body"] = req.data
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _Resp()

    with mock.patch("urllib.request.urlopen", fake):
        notify.send("🛡️ 파수꾼 매도 — 삼성전자 10주 · 하드 손절", critical=True)
    assert seen["url"].endswith("/stockbot-p0-xyz"), seen["url"]
    assert seen["method"] == "POST"
    # 본문은 UTF-8로 한글·이모지 보존
    assert "파수꾼".encode("utf-8") in seen["body"]
    # 헤더(Title/Priority/Tags)는 ASCII만 — latin-1 인코딩 크래시 방지
    for h in ("title", "priority", "tags"):
        seen["headers"][h].encode("ascii")   # 예외 안 나야 함
    print("[PASS] critical=True + 토픽 설정 → ntfy POST(본문 UTF-8·헤더 ASCII)")


def test_korean_title_would_not_crash():
    """Title에 한글을 넣어도(방어적 인코딩) latin-1 크래시가 안 나야 한다."""
    _clean_env()
    os.environ["NTFY_TOPIC"] = "t"
    from bot import notify
    with mock.patch("urllib.request.urlopen", lambda *a, **k: _Resp()):
        notify._ntfy("본문 한글", title="한글제목")   # ascii,ignore로 걸러짐
    print("[PASS] 한글 Title도 방어적 인코딩으로 크래시 없음")


def test_ntfy_failure_does_not_raise():
    _clean_env()
    os.environ["NTFY_TOPIC"] = "t"
    from bot import notify

    def boom(*a, **k):
        raise urllib.error.URLError("down")

    with mock.patch("urllib.request.urlopen", boom):
        # 예외가 send() 밖으로 새면 실패
        notify.send("🔴 손절", critical=True)
    print("[PASS] ntfy 다운도 send()는 조용히 반환(빌드 안 죽임)")


def test_trade_only_filters_noise_but_keeps_trade_query_and_critical():
    _clean_env()
    os.environ["NOTIFY_MODE"] = "trade_only"
    os.environ["TELEGRAM_BOT_TOKEN"] = "token"
    os.environ["TELEGRAM_CHAT_ID"] = "chat"
    from bot import notify
    sent = []
    with mock.patch.object(notify, "_tg_call",
                           lambda method, payload: sent.append(payload["text"]) or True), \
            mock.patch.object(notify, "_ntfy"):
        assert notify.send("오늘의 매수 제안")
        assert notify.send("성과 추적 시작")
        assert notify.send("🟢 KIS 매수 — AAPL", category="trade")
        assert notify.send("/보유 응답", category="query")
        assert notify.send("파수꾼 중단", critical=True)
    assert sent == ["🟢 KIS 매수 — AAPL", "/보유 응답", "파수꾼 중단"]
    print("[PASS] trade_only → 매매·조회·치명 경보만 전송")


if __name__ == "__main__":
    test_noncritical_never_calls_ntfy()
    test_critical_unset_topic_no_network()
    test_critical_with_topic_posts_ntfy()
    test_korean_title_would_not_crash()
    test_ntfy_failure_does_not_raise()
    test_trade_only_filters_noise_but_keeps_trade_query_and_critical()
    print("\n✅ ntfy 이중화 전부 통과 — P0만·독립·UTF-8본문·무해폴백.")
    _clean_env()
