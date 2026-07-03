"""텔레그램 알림 전송 — 표준 라이브러리만 사용(requests 의존성 없음).

환경변수 TELEGRAM_BOT_TOKEN·TELEGRAM_CHAT_ID가 없으면 '드라이런'으로 콘솔에만
출력 — 로컬 테스트나 시크릿 설정 전에도 나머지 로직을 검증할 수 있게.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


def send(text: str) -> bool:
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
    except urllib.error.URLError as e:
        print(f"[텔레그램 전송 오류] {e}")
        return False
