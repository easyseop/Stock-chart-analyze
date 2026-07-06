"""추천 성과 자동 채점 — '지금 진입' 추천이 이후 실제로 맞았는지 기록·집계.

매매 지표로 쓰는 이상 "이 추천이 과거에 얼마나 맞았나"를 눈으로 검증할 수 있어야
한다(사용자 요청). 정적 사이트 제약 안에서 포워드 테스트로 구현:

  매 빌드마다(추가 네트워크 0 — 이번 빌드의 현재가만 사용):
    ① 새 '지금 진입' 추천을 오픈 기록으로 추가(종목당 오픈 1건, 진입가=추천 시점가)
    ② 오픈 기록 갱신 — 목표 도달=승 / 손절 이탈=패 / 60일 경과=만료(±부호로 판정)
    ③ 승률·평균 R 집계 → 홈 요약 + public/api/track.json 공개

상태는 data_cache/track.json — 데이터 캐시와 함께 actions/cache로 영속.
"""
from __future__ import annotations

import datetime
import json
import os

STATE_PATH = os.path.join("data_cache", "track.json")
EXPIRE_DAYS = 60      # 캘린더 기준(≈40거래일) — 그때까지 결판 안 나면 부호로 마감


def _load() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fp:
            st = json.load(fp)
            st.setdefault("entries", [])
            return st
    except Exception:
        return {"entries": []}


def stats(st: dict | None = None) -> dict:
    st = st or _load()
    closed = [e for e in st["entries"] if e.get("status") in ("win", "loss")]
    wins = [e for e in closed if e["status"] == "win"]
    rs = [e.get("r", 0) for e in closed]
    n_open = sum(1 for e in st["entries"] if e.get("status") == "open")
    return {
        "closed": len(closed), "wins": len(wins), "losses": len(closed) - len(wins),
        "win_rate": round(len(wins) / len(closed) * 100) if closed else None,
        "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
        "open": n_open,
    }


def update(results: list[dict], picks: dict, out_dir: str) -> dict:
    """빌드 시 호출 — 기록 갱신 + public/api/track.json 발행. 집계 반환."""
    import config
    st = _load()
    today = config.today_kst().isoformat()
    px = {r["code"]: (r.get("sr") or {}).get("price") for r in results}
    open_codes = {e["code"] for e in st["entries"] if e.get("status") == "open"}

    # ① 신규 오픈(종목당 1건)
    for p in picks.get("now", []):
        if p["code"] in open_codes or not p.get("entry") or not p.get("stop"):
            continue
        st["entries"].append({
            "code": p["code"], "name": p["name"], "ccy": p["ccy"], "date": today,
            "entry": p["entry"], "stop": p["stop"], "target": p["target"],
            "status": "open",
        })
        open_codes.add(p["code"])

    # ② 오픈 기록 채점(이번 빌드의 현재가로)
    for e in st["entries"]:
        if e.get("status") != "open":
            continue
        cur = px.get(e["code"])
        if cur is None:
            continue
        risk = e["entry"] - e["stop"]
        e["last"] = round(float(cur), 4)
        e["r"] = round((cur - e["entry"]) / risk, 2) if risk > 0 else 0.0
        aged = (datetime.date.fromisoformat(today)
                - datetime.date.fromisoformat(e["date"])).days >= EXPIRE_DAYS
        if cur <= e["stop"]:
            e.update(status="loss", exit=e["last"], exit_date=today)
        elif cur >= e["target"]:
            e.update(status="win", exit=e["last"], exit_date=today)
        elif aged:                       # 만료 — 그 시점 손익 부호로 판정
            e.update(status="win" if e["r"] > 0 else "loss",
                     exit=e["last"], exit_date=today, expired=True)

    st["entries"] = st["entries"][-1000:]        # 무한 성장 방지
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as fp:
        json.dump(st, fp, ensure_ascii=False, indent=1)

    s = stats(st)
    os.makedirs(os.path.join(out_dir, "api"), exist_ok=True)
    with open(os.path.join(out_dir, "api", "track.json"), "w",
              encoding="utf-8") as fp:
        json.dump({"generated_at": config.now_kst()
                   .strftime("%Y-%m-%dT%H:%M:%S+09:00"),
                   "stats": s, "recent": st["entries"][-60:]},
                  fp, ensure_ascii=False, indent=1)
    return s
