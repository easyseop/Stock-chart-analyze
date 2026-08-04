# KIS 스캐너 직접진입 V3 — Codex V2 재검토(신규 P1·P2) 반영

- 직전 검토 대상: `1e0b3204` (Codex 판정: 병합 차단 — 신규 P1 1건·P2 1건)
- 이 수정의 코드 커밋: `b50589f3`
- 브랜치: `claude/kis-direct-scanner-entry`
- 판정 규칙: P0/P1 하나라도 있으면 병합 차단. 병합·Oracle 배포·allowlist
  설정·kill 하향은 재검토 통과 후에도 각각 별도 사용자 승인.

## P1 — 음수 손절가 통과 (수정)

Codex 재현: `entry=100, stop=-5` → `per_share = 100-(-5) = 105`(큰 양수) →
`gate=sent`, `order_meta.stop=-5.0`. 체결되면 `kis_accounting`(stop<=0 거부)·
`ledger.provisional_buy_protection`(stop<=0 제외)이 기록을 거부해 **실보유만
있고 보호가 없는** 상태가 된다.

수정 2겹:

1. `bot/kis_buyloop.py` 입력 게이트에 `stop <= 0` 추가 — A/B 공통 경로
   (`_now_signals`/`_shelf_cands` 이후 같은 run_once 게이트)이므로 양쪽 모두
   차단된다. 사유 문구도 "진입/손절가 무효(NaN·inf·0·음수)"로 갱신.
2. `bot/kis_buy.py execute_entry` 이중방어 — `order_meta`에 `stop` 키가
   **제공된 경우** float 변환 실패·비유한·`<= 0`을 input 게이트에서 거부.
   `stop` 키가 없는 order_meta(호환 경로)는 종전대로 통과. 이 방어는
   buyloop 외 호출자(`kis_pending`의 눌림 지정가 경로 — 자체 `stop < limit`
   검사는 음수 stop을 거르지 못했음)도 함께 보호한다.

회귀(요구 목록 전부, A/B 양쪽):

- `test_no_order_on_nan_zero_negative_or_inverted_stop`(확장) —
  `stop ∈ {0, -0.01, -5, -inf, NaN, entry(100), 120(역전)}` × {A/now, B/shelf}
  전부 실행기 호출 0회. 테스트명과 실제 검증 범위 일치. 양성 케이스
  (`0 < stop < order_px` → sent)도 함께 고정.
- `test_execute_entry_rejects_nonfinite_inputs`(확장) — 실행기 직접 호출에서
  `order_meta.stop ∈ {0, -5, NaN, -inf, "x"}` → input 차단, stop 키 없는
  메타·유효 stop은 input 통과.

## P2 — 명시적 fx=0의 기본값 부활 (수정)

`fx or settings.FX_USDKRW`는 0을 검사 전에 기본값으로 대체했다. Codex 권장
형태로 교체:

```python
raw_fx = settings.FX_USDKRW if fx is None else fx
try:
    fx = float(raw_fx)
except (TypeError, ValueError):
    return [{"code": "*", "gate": "input", "why": "환율 형식 오류"}]
if not math.isfinite(fx) or fx <= 0:
    return [{"code": "*", "gate": "input", "why": "환율 무효(NaN·inf·0·음수)"}]
```

**None만** 기본값을 쓰고, 명시적으로 전달된 값은 그대로 검증한다.

회귀: `test_invalid_fx_fails_closed_for_all_candidates` —
`fx ∈ {0, -1, NaN, inf, "invalid"}` → 실행기 0회·`gate=input`(비숫자도 예외
없이), `fx=None` → 기본값으로 정상 실행, `fx=1400.0` → `krw_per_usd=1400.0`
그대로 전달.

## 유지 확인

- V2에서 닫은 allowlist(env-존재-확정)·B freshness(`fresh is True`)·NaN
  현재가/목표가/실행기 입력 수정은 무변경 유지 — 해당 테스트 전부 통과.
- 변경 파일: `bot/kis_buyloop.py` · `bot/kis_buy.py` ·
  `tests/test_kis_buyloop.py` · `tests/test_kis_buy_gates.py`.
  그 외 안전 게이트 파일(`kis_orders`·`ledger`·`envelope`·`ownership`·
  `kis_boot`·`kis_accounting`·`kis_positions`·`signal_feed`·`kis_exits`·
  `sentinel`) 계속 무변경. `kis_buy.py` 변경은 input 게이트 강화뿐 —
  게이트 순서·기존 동작 약화 없음.

## 검증

```text
tests/test_*.py 49/49 통과
python -m compileall -q bot scanner tests scripts: 통과
git diff --check: 통과
실제 주문 HTTP 0건(주문 전송 전부 mock · buyloop urlopen 트랩)
```

## Codex V2 승인 조건 5항 대조

| # | 조건 | 상태 |
|---|---|---|
| 1 | `stop <= 0`을 A/B 공통 입력 경계에서 차단 | 수정(+실행기 이중방어) |
| 2 | 음수·0·NaN·inf·역전 stop 회귀를 실제 값으로 | 7값 × A/B 추가 |
| 3 | 명시적 fx 0·음수·비숫자 fail-closed(기본값 대체 금지) | 수정+테스트 |
| 4 | 전체 49 + 신규 테스트 통과 | 49/49 |
| 5 | 기존 allowlist·freshness·NaN 수정 유지 | 유지(테스트 통과) |

미해결 P0/P1/P2: 없음.
