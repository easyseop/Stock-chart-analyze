"""알림 전송 — 표준 라이브러리만 사용(requests 의존성 없음).

기본 채널: 텔레그램. 환경변수 TELEGRAM_BOT_TOKEN·TELEGRAM_CHAT_ID가 없으면
'드라이런'으로 콘솔에만 출력 — 로컬 테스트나 시크릿 설정 전에도 나머지 로직 검증.

P0(치명) 경보 이중화: send(text, critical=True)면 텔레그램과 **독립**으로 ntfy.sh
(무료·계정 불필요)에도 발행 → 텔레그램이 죽거나 스로틀돼도 손절·규칙위반 경보가
휴대폰에 도달(단일 알림 채널 장애 방어, RELIABILITY B4).
  · NTFY_TOPIC 미설정이면 ntfy는 전면 비활성 → 동작은 기존과 100% 동일.
  · 일상 알림(매수 제안·체결 요약 등)은 critical=False라 텔레그램만(ntfy 폭주 방지).
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

_HTML_TAG = re.compile(r"<[^>]+>")   # ntfy 본문용: 텔레그램 HTML 제거


def _ntfy(text: str, *, title: str = "P0 ALERT", priority: str = "urgent",
          tags: str = "rotating_light,warning") -> None:
    """P0 경보를 ntfy.sh로도 발행. NTFY_TOPIC 미설정 → 즉시 반환(네트워크 0).

    어떤 실패도 밖으로 던지지 않는다(텔레그램·빌드 영향 0).
    주의: HTTP 헤더는 latin-1만 허용 → Title/Priority/Tags는 반드시 ASCII로.
      한글·이모지는 **본문에만** 넣는다(본문은 UTF-8, 실측으로 이모지까지 OK).
    """
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    base = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    body = _HTML_TAG.sub("", text).strip().encode("utf-8")   # 본문=UTF-8
    req = urllib.request.Request(
        f"{base}/{topic}", data=body, method="POST",
        headers={"Title": title.encode("ascii", "ignore").decode() or "P0 ALERT",
                 "Priority": priority, "Tags": tags,
                 "Content-Type": "text/plain; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status >= 300:
                print(f"[ntfy 전송 실패] {resp.status}")
    except Exception as e:      # URLError 외 socket.timeout/TimeoutError/OSError도 흡수
        #   (감사 수정: 응답읽기 타임아웃은 URLError가 아니라 밖으로 새어, _ntfy가
        #    텔레그램 전송 앞이라 P0 경보가 양 채널 다 유실됐다. ntfy 실패는 무해.)
        print(f"[ntfy 전송 오류] {e}")


def send(text: str, *, critical: bool = False) -> bool:
    """텔레그램 전송. critical=True면 ntfy.sh로도 이중 발행(P0 경보).

    반환값은 항상 '텔레그램 성공 여부'(기존 호출부 의미 유지) — ntfy 결과는 무관.
    """
    if critical:
        _ntfy(text)          # NTFY_TOPIC 설정 시에만 발행(미설정=무동작·무네트워크)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[드라이런 — TELEGRAM_BOT_TOKEN/CHAT_ID 미설정]\n" + text + "\n")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            ok = json.load(resp).get("ok", False)
            if not ok:
                print(f"[텔레그램 전송 실패] {resp.status}")
            return ok
    except Exception as e:      # URLError 외 타임아웃/JSON오류도 흡수(감사 수정):
        print(f"[텔레그램 전송 오류] {e}")   # 응답읽기 타임아웃이 밖으로 새 호출부
        return False                          # (파수꾼 등)의 사이클을 중단시키지 않게
