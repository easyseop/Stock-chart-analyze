# 구현설계서 — 총위험 상한·이력 게이트·섀도 실험·정직화 (Codex 검토용)

작성: Claude, 2026-08-19 · 근거: 외부 평가(GPT) 보고서 + 사용자 지시
브랜치: `claude/happy-gauss-cwoq21` · 역할: **Codex 적대 검토 → 사용자 승인 → 병합·배포**

배경 문서: `docs/GPT_REVIEW_REQUEST_STRATEGY.md`(요청서). 평가 결론 중 사용자가
채택한 항목만 구현했다. **임계값 미세조정은 전부 배제**(표본 39건 — 다중검정 과적합).

---

## 0. 한눈에

| # | 항목 | 종류 | 핵심 파일 |
|---|---|---|---|
| 1 | 계좌 단위 총 open risk 상한 | **매수 차단 게이트(신규)** | `bot/risk_budget.py`, `bot/kis_buyloop.py` |
| 2 | A 후보 이력 게이트 | 후보 제외(수집 불변) | `scanner/gates.py`, `scanner/analyze.py` |
| 3 | B1 태그 + B2 섀도 + A ablation | **관측 전용(주문 0)** | `scanner/screener.py`, `scanner/gates.py` |
| 4 | 프로파일 근사 명시 | 메타데이터 | `scanner/analyze.py` |
| 5 | `holder_pnl` → `profile_pnl_proxy` | 개명 | `analyze`·`screener`·테스트 |

매도(파수꾼)·kill·원장 쓰기 경로는 **어디도 건드리지 않았다.**

---

## 1. 계좌 단위 총 open risk 상한 (`bot/risk_budget.py`)

### 무엇을
신규 매수 전에 **"지금 손절이 전부 맞으면 얼마 잃나"** 를 계산해 시드 대비
상한(기본 10%, `MAX_OPEN_RISK_FRACTION`)을 넘으면 신규 매수를 차단한다.

### 왜
거래당 1%·종목당 1/3 상한은 있는데 **합산** 상한이 없었다(외부검토 P0 지적).
1% 포지션 20개 = 동시 손절 시 20% — 각 매매는 규칙을 지켰는데 계좌는 크게
잃는 구조. 총위험은 "천천히 잃는" 전략 결함과 달리 "한 번에 잃는" 유일한 경로다.

### 어떻게
- 정의: `Σ max(0, entry − stop) × qty` (원장 `kis_positions` 기준, USD는 환율 환산).
  **래칫된 포지션(stop ≥ entry)은 0** — 지금 다 맞아도 그 포지션은 안 잃는다.
- 판정 위치: `kis_buyloop.attempt()`의 잔고 확인 직후, 후보 루프 전 1회.
  차단 시 전 후보 `gate=portfolio_risk`. A·B 슬리브 공통.
- fail-closed: 계량 불가 행(무보호 stop≤0·entry 유실·qty bool·ccy 유실)이
  하나라도 있으면 차단. 원장 읽기 실패도 차단. **위험을 모르는 채 더하지 않는다.**
- 시드(분모)는 `envelope.operating_total_krw()`(예산 게이트와 같은 축).
  시드=0 + 위험=0 → 허용(비율 0 — 시드 검증은 execute_entry 사이징 소관),
  시드=0 + 위험>0 → 차단.
- cap env: `(0,1]` 존중, **>1은 1로 클램프**(완화 요청을 기본값으로 되돌리지
  않음 — self-heal P2-1과 같은 방향 결함 방지), ≤0·비수는 기본값.
- 로그는 상태 **전환 시에만**(`_log_risk_gate`) — 60초 루프 소음 방지.

### 반복 중 잡은 반례(자기 점검 기록)
1. `ccy` 유실 행에 환율을 곱하면 KR 종목이 1380배 과대평가 → 사실상 영구 차단.
   → 계량 불가로 분류(차단은 동일하나 사유가 정직).
2. cap >1을 조용히 기본값(더 엄격)으로 되돌림 → 클램프로 수정.
3. 시드=0 차단이 기존 테스트 계약(execute_entry 모의) 파괴 → 위험 유무로 분기.

### 한계(알고 남긴 것)
- **원장 밖 고아(CVNA형)는 이 게이트가 못 본다** — `scripts/kis_orphan_audit.py`가 짝.
- 접수-미체결 주문의 예약 위험은 미합산(과소평가 방향). 후속 과제로 명시.
- 기본 10%는 관측용 초기값 — 배포 후 실제 수치 관측 뒤 사용자가 조정.

### 증거
`tests/test_risk_budget.py` 6건 PASS · 뮤테이션 6/6 KILLED
(M1 래칫 위험 산입 / M2 환율 미적용 / M3 무보호 fail-open / M4 경계 완화 /
M5 완화설정 무시 / M6 buyloop 게이트 무시).

---

## 2. A 후보 이력 게이트 (`gates.exclusion_reasons`)

### 무엇을
52주 범위 산정에 필요한 봉수(`NEWHIGH_LOOKBACK`=252) 미만이면 A 신호
**후보에서만** 제외. **수집·캐시·화면 노출·B는 불변**(B는 이미 504 게이트).

### 왜
`lb = min(len(d), 252)`가 짧은 이력을 조용히 "52주 범위"로 대체한다.
실측: 상장 1.5년 RHLD가 급등 이력만으로 "저점권" 매수신호(2026-08-18).
사용자 지시: "후보에서 제거하되 수집 리스트에선 제거하지 마라."

### 어떻게
`analyze` 결과에 `bars: len(d)` 추가 → `exclusion_reasons`에서
`bars < 252`면 사유 추가. **`bars` 키가 없는 구형 행은 판정하지 않는다**
(오탐 제외 방지 — 신형 스캔은 항상 키를 실으므로 실효 공백 없음).
bool 방어(`isinstance(bars, int) and not isinstance(bars, bool)`).

### 증거
`tests/test_shadow_signals.py::test_short_history_excluded_from_a_but_kept_in_results`
· 뮤테이션 M4(게이트 제거) KILLED.

---

## 3. 섀도 실험 — B1 태그 · B2 신호 · A ablation (`screener._signals_json`)

### 무엇을
셋 다 **관측 전용**: 자본 0 · 주문 0. 9월 리뷰의 원료.

- **B1** = 별도 스트림이 아니라 기존 shelf/shelf_watch 신호에
  `trend_above_200: true/false/null` 태그. B0 기록에서 B1(=B0+추세필터)을
  후처리로 재구성할 수 있으므로 중복 발행은 낭비다. **단일 변수(추세 필터
  하나)의 순수 효과** 측정용.
- **B2** = 별도 전략 계열(추세 눌림목): 200일선 위 + 200일선 기울기>0 +
  50일선 ±5% 이내 되돌림 + 반등 확인(상단마감·거래량 — B와 동일 기준) +
  하드제외(`exclusion_reasons`) 통과. 진입=현재가, 손절=최근3봉저점−2%,
  목표=2R — **B와 같은 손절·목표 구조**로 비교 가능성 확보.
  `group="shelf_shadow_b2"`, `shadow: true`, `orderable: false`. 상위 10건.
- **A ablation** = 정확히 게이트 **하나**에서만 떨어진 now-후보를
  `{code, gate(rp|runup|consensus), range_pos, runup63, stage, price}`로
  최상위 `a_ablation` 키에 기록(상위 20건). "그 게이트가 없었으면 어떤 매매가
  생겼나"의 되감기 원료. `stop` 게이트는 안전 사이징이라 실험 대상에서 제외.

### 왜
외부검토 5.2: 제안했던 B′는 두 변수를 동시에 바꿔 원인 귀속이 불가능했다.
B0(기준)/B1(추세 필터만)/B2(별도 계열)로 쪼개야 단일 변수 실험이 된다.
A ablation은 외부검토 3.4의 "필터 기여도 분리 불가" 지적에 대한 응답.

### 어떻게 — 주문 격리가 구조로 보장되는 근거
매수루프 후보 선별은 `_now_signals`(group=="now")·`_shelf_cands`(group=="shelf")
**완전일치 필터**다. 섀도 그룹은 어느 쪽에도 못 들어간다 — 테스트가
`kis_buyloop._shelf_cands(payload)==[]`를 직접 단언하고, 뮤테이션 M2(섀도를
shelf 그룹으로 발행)가 KILLED임을 확인했다.

### 반복 중 잡은 반례
1. **`_signals_json`이 `frames_map`을 안 받는데 참조** — ast는 통과하지만
   런타임 NameError로 **스캔 전체가 죽는 결함**. 시그니처에 옵션 인자 추가,
   None이면 B2만 조용히 비활성(fastlane·oracle_brain 경로 — 섀도는 15분
   전체 스캔에서만 나온다. 의도된 강등).
2. `frames_map` 키가 `"d"`가 아니라 `"D"` — B2가 영원히 빈 결과가 될 뻔.
3. **classify는 조기 반환이라 "사유 1개 ≠ 게이트 1개 탈락"** — rp·runup 둘 다
   걸린 후보가 ablation에 오염 유입. `gates.a_gate_failures()`(독립 판정
   전용 함수) 신설로 해결. 뮤테이션 M5(조기반환 재도입) KILLED.

### 증거
`tests/test_shadow_signals.py` 5건 PASS · 뮤테이션 5/5 KILLED
(M1 추세조건 제거 / M2 주문가능 그룹 발행 / M3 다중탈락 허용 /
M4 이력게이트 제거 / M5 조기반환 재도입).

---

## 4. 프로파일 근사 명시 (P1-2)

### 무엇을
shelf 신호에 `profile_method: "ohlcv-uniform-approx"`와
`profile_bin_pct`(bin 폭의 현재가 대비 %)를 싣는다. **임계값은 안 바꿨다.**

### 왜
`supply.volume_profile`은 하루 거래량을 그날 [저가,고가]에 **균등 배분**하는
근사다(일봉엔 가격대별 체결 분포가 없다). 그런데 B는 "POC 5% 이내 터치"라는
정밀 조건을 건다 — bin 24개면 bin 하나가 범위의 ~4%라서 **판정 정밀도가
입력 정밀도를 초과**한다(외부검토 4.2-3). 지금 임계값을 조정하면 39건
최적화가 되므로, 이번 단계는 **소비자가 오판하지 않게 사실을 싣는 것**까지만.

### 증거
`test_shelf_gates` PASS(메타 필드 검증 포함) · 실측 검증: bin_pct=1.25
(edges 90~120·24bin·price 100 → (120-90)/24/100 = 1.25%).

---

## 5. `holder_pnl` → `profile_pnl_proxy` (P2)

과거 거래량 분포는 **현재 보유자의 취득원가가 아니다**(회전 중복 계상·보유
여부 미상 — 외부검토 2.3). "평균 보유자 손익"으로 읽히는 이름을 버리고
proxy로 개명. 신호 payload·analyze·테스트 전부 동기. 옛 키는 어제(8/18)
추가돼 소비자가 없으므로 호환 유지 없이 제거. 테스트가
`"holder_pnl" not in ...`을 단언한다.

---

## 6. Codex 검토 요청 포인트(적대적으로 봐 달라)

1. **risk gate의 fail-closed가 과한가**: 계량 불가 1건으로 전 슬리브 신규
   매수가 멈춘다. 대안(해당 종목만 제외하고 나머지 합산)과 비교해 판정하라.
   우리 논리: 원장 오염은 국소 이상이 아니라 시스템 신뢰 문제(CVNA 실측).
2. **기본 cap 10%의 위험**: 현 보유(54종목)의 실제 open risk를 모른 채 정한
   초기값이다. 배포 직후 게이트가 상시 차단이면 매수가 전면 정지된다 —
   배포 절차에 "첫 사이클 로그 확인" 단계를 넣었는지 검증하라.
3. **B2 조건의 look-ahead 여부**: 모든 입력이 당일 종가 기준인지(미래 참조
   없음) 코드로 확인하라. 특히 `checks`(상단마감·거래량)는 B용으로 계산된
   값을 재사용한다 — B 게이트(504봉·범위)가 B2 후보를 부당하게 좁히는
   결합이 없는지 볼 것 (`_shelf_signal`이 이력 부족으로 조기 반환하면
   checks가 비어 B2도 죽는다 — 이건 의도인가? 우리 답: B2도 근사 프로파일
   기반 반등 확인을 쓰므로 같은 이력 요구가 정당하다. 반박 환영).
4. **ablation의 선택 편향**: 단일 탈락만 기록하므로 "게이트 2개를 동시에
   완화하면"의 효과는 측정 불가 — 의도된 한계인지 결함인지.
5. 섀도 신호가 공개 signals.json 크기·사이트 렌더링에 주는 영향(캡 10/20으로
   묶었지만 소비자 JS가 미지 그룹에서 깨지지 않는지 — test_site_app는 PASS).

## 7. 검증 총괄

- 신규 테스트: risk_budget 6 · shadow_signals 5 (+기존 보강 2)
- 뮤테이션: **11/11 KILLED** (§1 6건 + §3 5건)
- 회귀: 16모듈 PASS(buyloop·buy_gates·signal_feed·site_app·shelf·gate_audit·
  sentinel·kill_self_heal·notify·ops_status 등) · compileall OK
- 배포 주의: 코드만으로 동작(새 env 불요). `MAX_OPEN_RISK_FRACTION`은 선택.
  **배포 후 첫 장중 사이클에서 `journalctl -u buyloop | grep 총위험` 확인** —
  상시 차단이면 cap 상향 또는 원장 정리가 먼저다.
