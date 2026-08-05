# KIS 스캐너 직접진입 V4 — Codex V3 재검토(unknown tactic P1·stop=0 P2) 반영

- 직전 검토 대상: `b50589f3` (Codex 판정: 병합 차단 — 신규 P1 1건·P2 1건)
- 이 수정의 코드 커밋: `a4b2d8e1`
- 브랜치: `claude/kis-direct-scanner-entry`
- 판정 규칙: P0/P1 하나라도 있으면 병합 차단. 병합·Oracle 배포·allowlist
  설정·kill 하향은 재검토 통과 후에도 각각 별도 사용자 승인.

## P1 — 알 수 없는 전술명의 tolerance 우회 (수정)

Codex 재현: `tactic.mode="banana"`(또는 `"FULL"`) + `cur=200, entry=100` →
full/half tolerance와 half/pullback 눌림가 검사 전부 우회 → `order_px=cur`
→ `gate=sent`, 주문가 200. 승인 진입가에서 100% 이탈한 주문이 실행기 도달.

수정(`bot/kis_buyloop.py` run_once):

```python
raw_mode = (tactic.get("mode") if isinstance(tactic, dict) else tactic)
mode = str(raw_mode or "full").strip().lower()
if mode not in ("full", "half", "pullback"):
    results.append({"code": code, "gate": "tactic",
                    "why": f"알 수 없는 진입 전술({mode})"}); continue
```

- 정책: Codex 권장안 채택 — **공백 제거·소문자 정규화 후 허용 집합 검사**.
  `"FULL"`·`" full "`은 정상 `full`로 정규화되어 tolerance가 적용되고,
  집합 밖은 전부 `gate=tactic` 차단. 알 수 없는 mode가 current-price full로
  폴백하는 경로는 없다.
- 위치: entry/stop 파싱 직후·pb 파싱 전 — 이후의 tolerance/눌림가/손절폭
  게이트는 검증된 3개 모드에 대해서만 실행된다.

회귀(`test_unknown_tactic_never_bypasses_tolerance`, A/now·B/shelf 양쪽):

- `mode ∈ {"banana", "unknown", ["x"], {"weird":1}}` × `cur=200` →
  실행기 0회·`gate=tactic`
- `mode ∈ {"FULL", " full ", "Full"}` × `cur=200` → `gate=tolerance`(정규화
  후 tolerance 차단), × `cur=100.4` → 정상 `sent`
- `mode="PULLBACK"` + 유효 pb=97 → 지정가 97로 실행기 전달(계약 보존)

## P2 — stop=0의 무진단 소멸 (수정)

`_now_signals`/`_shelf_cands`의 truthy 필터(`s.get("stop")`)가 `stop=0`을
후보 단계에서 조용히 제거해 요구된 `gate=input` 진단이 남지 않았다.

수정: 두 필터 모두 **키 존재만** 확인(`"entry" in s and "stop" in s`)하고
숫자 유효성은 run_once의 공통 input 게이트가 판정 — 0·음수·NaN·inf·문자열
전부 같은 경계에서 일관된 `gate=input`을 남긴다.

회귀 강화: `test_no_order_on_nan_zero_negative_or_inverted_stop`이 이제
`not sent`가 아니라 **각 종목 결과의 `gate == "input"`을 직접 단언**한다
(`stop ∈ {0, -0.01, -5, -inf, NaN, entry, 역전}` × A/B 전부).

## 유지 확인

- V1~V3에서 닫은 수정 전부 무변경 유지: allowlist env-존재-확정 · B
  `fresh is True` · NaN 현재가/목표가/환율 · 음수 stop(입력 게이트+실행기
  이중방어) · fx None-only 기본값. 해당 테스트 전부 통과.
- 변경 파일: `bot/kis_buyloop.py` · `tests/test_kis_buyloop.py` 2개뿐.
  `kis_buy.py` 포함 안전 게이트 파일 전부 무변경.

## 검증

```text
tests/test_*.py 49/49 통과
python -m compileall -q bot scanner tests scripts: 통과
git diff --check: 통과
실제 주문 HTTP 0건(주문 전송 전부 mock · buyloop urlopen 트랩)
```

## Codex V3 승인 조건 6항 대조

| # | 조건 | 상태 |
|---|---|---|
| 1 | tactic.mode 정규화 + 허용 집합 밖 주문 전 차단 | 수정 |
| 2 | A/B 양쪽 unknown tactic tolerance 우회 회귀 | 4무효값×A/B + 변형 3종 추가 |
| 3 | stop=0이 공통 input 게이트 도달·gate=input 기록 | 수정 |
| 4 | invalid stop 테스트가 정확한 gate==input 단언 | 강화 |
| 5 | V1~V3 수정(allowlist·freshness·NaN·음수 stop·FX) 유지 | 유지(테스트 통과) |
| 6 | 전체 49 + 신규 테스트 통과 | 49/49 |

미해결 P0/P1/P2: 없음.
