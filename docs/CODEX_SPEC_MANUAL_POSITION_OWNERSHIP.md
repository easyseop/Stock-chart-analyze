# 구현지시서 — 수동 매수 포지션 귀속 정정 + 신규 게이트·섀도 적대 검토

작성: Claude, 2026-08-19 · 역할: **Codex 구현/검토 → Claude 적대 재검토 → 사용자 승인 후 병합·배포**
전제: 이 문서는 매도 보호(파수꾼)·kill 상향 규칙을 바꾸지 않는다.

---

## T1. 적대 검토 — 총위험 게이트·이력 게이트·섀도 실험 (우선)

대상: 커밋 `6708f7bc`(구현) + `b75bce3c`(설계서).
검토 자료: `docs/IMPL_DESIGN_RISKCAP_SHADOW.md` — §6에 공격 포인트 6개를
지정해 뒀다(§6-6은 아래 T2로 대체·재정의됨). 판정은 P0/P1/P2/P3로,
증거는 실패 테스트명 + 종료코드 원문. 뮤테이션 재검증 환영(커밋 먼저).

## T2. CVNA 귀속 정정 — "봇 포지션"이 아니라 "사용자 수동 매수"였다

### 사실관계 (2026-08-19 확정)

- CVNA 74주 @ $65.03은 **사용자가 KIS 앱에서 직접 산 것**(H3 확인). 봇 주문
  아님 — 그래서 원장·costbook·거래이력 어디에도 없는 게 **정상**이었다.
- baseline(기보유 denylist)은 arming 시점 캡처라 **그 이후의 수동 매수**를
  모른다. 이 간극이 이번 사고의 진짜 뿌리다.
- 임시조치 이력: ① stop=60.48로 kis_positions에 **봇 포지션인 것처럼 복원**
  (무보호 해소 목적) ② SEED A 30M→37M 상향(불변식 해소). 둘 다 H3 확인
  전의 조치라 **귀속이 틀렸다** — 사용자 돈을 봇 장부에 편입한 상태다.

### 요구사항

R1. **CVNA를 baseline으로 이관**한다:
  - `ownership` baseline에 CVNA 추가(파일 스키마 유지·원자적 쓰기·0600).
    단발 스크립트가 아니라 **재사용 가능한 CLI**로:
    `python -m bot.kis_arm --adopt CVNA "사유"` 또는 동급. 수동 매수는
    또 생긴다 — 도구가 있어야 다음번엔 1분짜리 일이 된다.
  - kis_positions에서 CVNA를 close 이벤트로 제거(복원 open의 역연산).
    멱등: 두 번 실행해도 안전.
  - 동결(frozen) 해제 여부는 **건드리지 않는다**(별도 운영 판단).
R2. 이관 후 **SEED A를 30M으로 원복**해도 불변식이 성립함을 검증하는
  절차를 문서화한다(브로커-진실 held_cost가 baseline 제외로 계산되는지
  코드로 확인 — `_broker_state`/`aggregate`의 baseline 필터 경로).
R3. 재발 대응 계약: 브로커에 있고 원장·baseline 어디에도 없는 종목
  (= 고아)은 **자동 편입하지 않는다**. 분류(봇 포지션 복원 vs baseline
  이관)는 사람의 결정이다 — 감지·경보는 Claude의 포지션 대사 자동화가
  맡는다(별도 작업, 이 지시서 범위 밖). 이 지시서에서는 R1의 CLI가
  그 결정의 실행 도구가 되는 것까지만.
R4. §6-6(costbook 소급 기입)은 **철회**한다 — H3이므로 봇 회계에 CVNA
  원가를 넣는 것 자체가 오귀속이다. 대신 R1 이관 시 costbook에 아무
  흔적도 남지 않음을 테스트로 증명하라(매도 시 현금 부풀림 경로 소멸).

### 테스트 (최소)

1. adopt CLI: baseline에 추가 + kis_positions close + 멱등(2회 실행 동일).
2. adopt 후 buy_denied("CVNA") = True(기보유 denylist).
3. adopt 후 파수꾼 held 집계에서 CVNA 제외(감시·매도 안 함).
4. adopt 후 costbook·open_risk에 CVNA 흔적 0.
5. baseline 파일 손상·쓰기 실패 → 기존 fail-closed 유지(전 종목 매수 거부).
6. 기존 스위트 무손상(test_ownership_baseline·test_kis_buyloop·
   test_risk_budget·test_sentinel).

### 주의 (적대 검토 포인트)

- baseline은 "매수 전면 거부"의 근거 파일이다 — 쓰기 실패가 조용히 지나가면
  전 종목 거부(과차단) 또는 denylist 누락(과허용) 둘 다 가능. 원자적 쓰기 +
  실패 시 명시적 에러.
- adopt가 kis_positions close를 먼저 하고 baseline 추가가 실패하면 CVNA가
  다시 무보호 고아가 된다 — **순서: baseline 추가 성공 확인 → close**.
- 뮤테이션 검증 전 커밋. 증거는 실패 테스트명 + 종료코드 원문.

## 완료 기준

T1 판정문 + T2 구현·테스트·뮤테이션 증거 → Claude 적대 재검토(P0/P1=0) →
사용자 승인 → 병합. 배포 후: 사용자가 adopt 실행 → SEED 원복 → 붙박이
확인(`python3 scripts/kis_orphan_audit.py` = 이상 없음).
