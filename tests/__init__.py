"""테스트 패키지 공통 안전장치 — 운영 알림 자격증명을 절대 자동 로드하지 않는다."""
from __future__ import annotations

import os


# 원격 운영 서버에서 `python -m tests.test_*`를 실행해도 ~/kis.env나
# /etc/stock/kis.env의 실제 알림 채널로 테스트 문구가 발송되면 안 된다.
os.environ["NOTIFY_ENV_FALLBACK"] = "0"
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""
os.environ["NTFY_TOPIC"] = ""
