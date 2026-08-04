# TWR·지수 비교 격리 V5 — Codex V4 판정 반영 검토 요청

- 구현 커밋: `85b9c29e` (기반: `8bc54025` + 검토요청 `34347da4`)
- 범위: **TWR·성과/지수 누적 비교만** — 매수경로·KIS L0/L1 판단과 무관
- 판정 규칙: P0/P1 하나라도 있으면 병합·배포 차단
- V4 판정: ⛔ P1 2건 · P2 3건 · P3 1건 → 이번 커밋에서 전부 수정

## 0. 핵심 불변식(변경 없음)

> 알 수 없는 지수 수익률을 어떤 소비자 경로에서도 숫자로 만들지 않는다.
> JS에서 `x - null`은 조용히 `x - 0`이 된다 — null 산술은 전부 공용 순수
> 함수를 통해서만 한다.

## 1. P1-1 — perf.html 오늘 카드의 `null - 0` 뺄셈

- 수정: `scanner/templates/perf.html`
  - `diff = PM.todayIndexDiff(last[1], last[2])` — 계좌·지수 **둘 다** 유효할
    때만 숫자. `todayIndexDiff`는 `scanner/site_app/portfolio_math.js`의
    순수 함수(`optionalNumber` 기반, null/undefined/비유한 → null).
  - `diff === null`이면 기존 마크업이 "판정 보류" + `cls(null) = ""`(색상
    없음)을 이미 출력 — 값 계산만이 결함이었다.
  - perf.html이 `app/portfolio_math.js`를 `<script src>`로 로드한다(같은
    Pages 배포물 — CI Static build가 존재를 보증).
- 테스트(`tests/site_math.test.js`, 실제 JS 입력→출력):
  - `todayIndexDiff(1.25, null) === null` (V4 재현 입력)
  - `(null, 1.0) → null` · `(1.25, 0.5) → 0.75` · `(0, 0) → 0`
- 누적 알파도 같은 모듈의 `cumulativeAlphaSeries`로 이동(수학 동일 —
  aw/iw 복리, 결측 이후 segment 단절). Node 검산:
  `+10,+10/0,0 → +21` · `+10,-10/0,0 → -1` · `+10,+10/+10,-10 → +22` ·
  `지수 +2,null,+3 → [숫자, null, null]`(연결 금지).

## 2. P1-2 — previous_close 결측 지수의 0% 세탁

### 2.1 생산자(bot/alpha.py)

- `idx0` 초기화: carry(`basis=previous_close`) 세션에서 전일종가 없는 지수는
  기준을 **현재값으로 대체하지 않고 `None`**으로 둔다.
- 세션 수익률 계산: `idx0[name]`이 없으면 —
  - carry 세션: 그 틱의 `idx_previous_close`에 늦게라도 값이 오면 **전일종가**
    를 기준으로 고정(현재값 아님 — "다음 틱 복구" 반례 5), 없으면
    `ipct[name] = None`.
  - first_sample 세션: 종전대로 그 틱을 0% 기준(계약 유지).
- `daily_indices`: 전일종가 없는 지수를 **키 생략이 아니라 명시적 `None`**으로
  기록 — 키가 빠지면 소비자 폴백이 기준 다른 값을 넣는다.
- 주 지수 `series[][2]`·마감 `days[].idx`·`days[].indices`: `ipct`의 None이
  그대로 흘러 None 유지(기존 V4 경로 재사용).
- `dashboard_snapshot()`: `indices`/`daily_indices` 직렬화에서
  `float(None)` 없이 None 보존. `daily_indices` 키는 **원본 행에 있을 때만**
  내보낸다 — 구버전 행과 "명시적 결측" 행을 소비자가 구분할 수 있게.
- `_vs_line`: 주 지수 None이면 "지수 대비 판정 보류(지수 미확정)" — 백엔드
  알림에도 같은 뺄셈 결함이 있었다(대칭 수정).

### 2.2 소비자(scanner/site_app/app.js)

- `row.daily_indices?.[name] ?? row.indices?.[name]` 제거 →
  `PortfolioMath.dailyIndexValue(row, name)`:
  - `daily_indices` 키가 있는 행: `hasOwnProperty`로 확인, **명시적 null을
    세션 값으로 폴백하지 않음**. 행에 없는 지수 이름도 null.
  - `daily_indices` 키 자체가 없는 **구버전 행만** 세션 `indices` 폴백 허용.
- 결측 카운트도 같은 모듈의 `incompleteCount`로 통일(기간 누적 차단은 기존
  keyIncomplete 로직 그대로).

### 2.3 테스트

- Python(`tests/test_alpha.py`):
  - `test_missing_previous_close_never_becomes_zero_percent` — carry + 나스닥
    전일종가 결측 + S&P500 정상 → 나스닥만 None(세션/일간/시리즈/스냅샷),
    S&P500 +2%. 다음 틱 전일종가 복구 → **전일종가 기준 +1%**(현재값 0% 아님).
  - `test_snapshot_omits_daily_indices_for_legacy_rows_only` — 구버전 행은 키
    없음(폴백 허용 표식), 새 행은 `{"나스닥": None}` 보존.
- JS: `dailyIndexValue` — 명시적 null 유지 · 새 행의 누락 이름 null ·
  구버전 행만 폴백 · `daily=null, session=+1 → null`(반례 6).

## 3. P2-1 — hold 단계 account 노출

- `_twr_step`: hold(pending) 키 집합을 `day["pending_keys"]`로 계산하고
  A/B 중 하나라도 hold면 account도 포함. `shown()`은
  `unresolved ∪ pending_keys`를 전부 None(검증 중)으로 표시 — **내부
  wealth·기준선 갱신은 그대로**라 hold 해소 시 숫자로 복구된다.
- 부수 효과: 장중 알림(`_vs_line`)·series·series_v2가 hold 동안 자동으로
  "판정 보류"가 된다. series_v2 point에 `pending` 키 추가(소비자 표기용).
- 계약 변경으로 기존 테스트 4곳의 hold-표시 기대를 갱신
  (`observed_cliffs`·`persistent_anomaly`·`one_sleeve`·`first_gap`·
  `zero_nonfinite` — 각각 "hold 중 None + wealth 무누적" 단언으로 강화).

## 4. P2-2 — "전체" 400일 절단

- 권장안 채택: **append-only 일별 장기 원장** `bot/alpha_days.jsonl`
  (`ALPHA_DAYS_LEDGER_PATH`, fsync, 실패 1회 경보, gitignore) — 마감마다
  state days[] append **직후** 영속하므로 400일 창 밖으로 밀려도 원본이 남는다.
  `test_close_appends_to_long_term_days_ledger`.
- state 창은 `DAYS_RETENTION=400` 상수로 명시.
- UI 라벨(최소안 병행): `app.js` 성과 차트 노트에 range=전체일 때
  "전체 = {earliest}부터 최근 N거래일"을 표시하고, 창이 가득 찼으면
  "400거래일 창 — 창 밖 과거 이력은 서버 장기 원장(alpha_days)에 보존되며
  화면 누적에는 포함되지 않습니다"를 명시한다.
- Oracle 개인 API가 원장을 직접 읽는 확장은 후속(원장 포맷은 days 행 그대로).

## 5. P2-3 — 프런트 수학 자동 테스트

- 순수 함수 4종을 `scanner/site_app/portfolio_math.js`(기존 공용 모듈,
  UMD·Node require 가능)로 분리: `todayIndexDiff` ·
  `cumulativeAlphaSeries` · `dailyIndexValue` · `incompleteCount`.
- perf.html·app.js가 **같은 모듈을 소비**하고, `tests/site_math.test.js`가
  V4 최소 입력 전부를 실행 검증(15/15). CI(site-ui-ci)가 `node --test`로
  이미 실행한다 — 수동 검산 의존 제거.

## 6. P3-1 — 마감 알림 재시도의 시간 기준화

- `_deliver_close_alert`: `close_alert_next_at`(+5분)·
  `close_alert_first_fail_at` 저장. 간격 미도달 호출은 소모 없이 반환(빠른
  buyloop 주기가 12회를 12분에 소진하던 문제). 최초 실패 후 **1시간 경과**
  시에만 포기 — 포기 시 운영 경보 + 품질 원장 `close_alert_giveup` 기록,
  `close_alert_body`는 forensic 목적으로 보존(문서화된 계약).
- `test_close_alert_retry_is_time_based_not_count_based` — 1분 뒤 호출 무소모 ·
  6분 뒤 실제 재시도 · 1시간 뒤 포기+기록+본문 보존.

## 7. 검증 결과

```text
Python 독립 테스트 49/49 통과 (신규 5건 포함)
node --test tests/site_math.test.js: 15/15 통과 (신규 5 test 블록)
node --check app.js·portfolio_math.js: 통과
perf.html 인라인 script 구문 검사(node --check): 통과
python -m scanner.siteapp 정적 빌드: 통과 (app/portfolio_math.js 포함)
python -m compileall / git diff --check: 통과
```

변경 파일: `bot/alpha.py` · `scanner/site_app/app.js` ·
`scanner/site_app/portfolio_math.js` · `scanner/templates/perf.html` ·
`tests/site_math.test.js` · `tests/test_alpha.py` · `.gitignore` —
주문 전송·매수 게이트·kill·원장 상태기계 무접촉(V4 §주문 경로 영향과 동일).

## 8. V4 재검토 승인 조건 대조

| 조건 | 상태 |
|---|---|
| 지수 계약 미충족 시 None/판정 보류 | §2 — 생산·직렬화·소비 전 구간 |
| JS 소비자가 null을 0처럼 산술하지 않음 | §1·§5 — 공용 순수 함수 강제 |
| basis 다른 값 비교 금지 | §2 — 현재값 기준 대체 제거, 폴백 제한 |
| 명시적 null이 session 값으로 대체 안 됨 | §2.2 — hasOwn + 키 유무 구분 |
| hold account 확정 숫자 노출 없음 | §3 |
| 반례 포함 Node 테스트 통과 | §5 — 15/15 |
| Python 49개 회귀 통과 | §7 |
| TWR와 매수경로 분리 유지 | 이 커밋은 TWR 파일만 · 매수 재작업은 `claude/kis-direct-scanner-entry` 별도 브랜치 |
