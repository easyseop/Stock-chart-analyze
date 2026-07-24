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

_ENV_LOADED = False
_ENV_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NTFY_TOPIC", "NTFY_SERVER",
             "NOTIFY_MODE")
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?(" + "|".join(_ENV_KEYS) + r")\s*=\s*(.*)$")


def _ensure_env() -> None:
    """알림 자격증명 폴백 — os.environ에 토큰이 없으면 kis.env를 직접 파싱해 채운다.

    systemd `EnvironmentFile`은 `export KEY=val` 형식을 못 읽어(실측: telegram·
    buyloop 프로세스에 토큰 미주입 → 매수 알림 조용히 유실) 이 폴백이 필요하다.
    1회만 시도(_ENV_LOADED). 이미 설정된 값은 안 덮음(setdefault). 실패는 무시.
    값은 절대 로그·예외에 담지 않는다."""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    for p in (os.environ.get("AUTODEPLOY_ENV"), os.environ.get("BEACON_ENV"),
              "/etc/stock/kis.env", os.path.expanduser("~/kis.env")):
        if not p:
            continue
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    m = _ENV_LINE.match(line)
                    if m:
                        v = m.group(2).strip().strip('"').strip("'")
                        if v:
                            os.environ.setdefault(m.group(1), v)
        except OSError:
            continue
        if os.environ.get("TELEGRAM_BOT_TOKEN"):
            break


def _category(text: str, category: str | None = None) -> str:
    """기존 호출부도 운영 필터를 지키도록 텍스트를 보수적으로 분류한다."""
    if category:
        return str(category).strip().lower()
    plain = _HTML_TAG.sub("", text)
    if any(word in plain for word in (
            "KIS 매수", "KIS 익절", "KIS 타임스탑", "KIS B 목표",
            "파수꾼 매도", "체결 확정", "주문 응답 불명", "주문 확정")):
        return "trade"
    if any(word in plain for word in (
            "매수 제안", "도달 제안", "신호가", "제안 알림")):
        return "advice"
    if any(word in plain for word in (
            "성과", "세션 추이", "알파", "추적 시작")):
        return "report"
    return "ops"


def _allowed(text: str, *, critical: bool = False,
             category: str | None = None) -> bool:
    """NOTIFY_MODE에 따라 알림 허용 여부를 결정한다.

    all(기본): 기존 동작. trade_only: 실제 매매·사용자 조회·치명 안전 경보만.
    critical_only: critical=True인 안전 경보만. 잘못된 값은 안전하게 all로 처리한다.
    """
    _ensure_env()
    mode = os.environ.get("NOTIFY_MODE", "all").strip().lower()
    if critical:
        return True                              # 치명 안전 경보는 어떤 모드에서도 보존
    if mode == "trade_only":
        return _category(text, category) in ("trade", "query")
    if mode == "critical_only":
        return False
    return True


def _ntfy(text: str, *, title: str = "P0 ALERT", priority: str = "urgent",
          tags: str = "rotating_light,warning") -> None:
    """P0 경보를 ntfy.sh로도 발행. NTFY_TOPIC 미설정 → 즉시 반환(네트워크 0).

    어떤 실패도 밖으로 던지지 않는다(텔레그램·빌드 영향 0).
    주의: HTTP 헤더는 latin-1만 허용 → Title/Priority/Tags는 반드시 ASCII로.
      한글·이모지는 **본문에만** 넣는다(본문은 UTF-8, 실측으로 이모지까지 OK).
    """
    _ensure_env()
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


def send(text: str, *, critical: bool = False, category: str | None = None) -> bool:
    """텔레그램 전송. critical=True면 ntfy.sh로도 이중 발행(P0 경보).

    반환값은 항상 '텔레그램 성공 여부'(기존 호출부 의미 유지) — ntfy 결과는 무관.
    """
    _ensure_env()            # env에 토큰 없으면 kis.env에서 폴백 로드(매수 알림 유실 방지)
    if not _allowed(text, critical=critical, category=category):
        print(f"[알림 억제] mode={os.environ.get('NOTIFY_MODE')} "
              f"category={_category(text, category)}")
        return True                              # 호출부가 같은 알림을 재시도하지 않게 소비 처리
    if critical:
        _ntfy(text)          # NTFY_TOPIC 설정 시에만 발행(미설정=무동작·무네트워크)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    return _tg_call("sendMessage", {
        "chat_id": chat_id, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }) if token and chat_id else _dry(text)


def send_photo(photo_url: str, caption: str = "",
               *, category: str | None = None) -> bool:
    """사진 전송(그래프 등) — photo_url을 텔레그램이 직접 fetch. 실패=False.

    호출부는 False면 send(텍스트)로 폴백할 것(그래프 없이도 정보는 전달)."""
    _ensure_env()
    if not _allowed(caption, category=category):
        print(f"[알림 억제] mode={os.environ.get('NOTIFY_MODE')} "
              f"category={_category(caption, category)}")
        return True
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return _dry("[사진] " + caption)
    return _tg_call("sendPhoto", {
        "chat_id": chat_id, "photo": photo_url,
        "caption": caption, "parse_mode": "HTML",
    })


def _dry(text: str) -> bool:
    print("[드라이런 — TELEGRAM_BOT_TOKEN/CHAT_ID 미설정]\n" + text + "\n")
    return False


def _tg_call(method: str, payload: dict) -> bool:
    """텔레그램 API 공통 호출(sendMessage/sendPhoto). 어떤 실패도 밖으로 안 던짐."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/{method}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            ok = json.load(resp).get("ok", False)
            if not ok:
                print(f"[텔레그램 {method} 실패] {resp.status}")
            return ok
    except Exception as e:      # URLError 외 타임아웃/JSON오류도 흡수(감사 수정):
        print(f"[텔레그램 {method} 오류] {e}")   # 응답읽기 타임아웃이 밖으로 새 호출부
        return False                          # (파수꾼 등)의 사이클을 중단시키지 않게
