# 구현지시서 — 배포 유예창(deploy grace) + 즉시 운영: L0 복구

작성: Claude, 2026-08-11 08:2x KST · 발주: 사용자 지시("코덱스 지시서 만들어줘")
역할: **Codex 실행/구현 → Claude 적대 검토 → 사용자 승인 후 병합·배포** (기존 절차)

---

## 0. 즉시 운영 작업 — kill L1 → L0 복구 (사용자 승인 완료)

사용자 승인: 2026-08-11 08:1x, 이번 1회 하향에 한함.

배경: 2026-08-10 23:34 KST, autodeploy가 sentinel을 재시작하는 동안 heartbeat
공백이 120초를 넘겨 watchdog이 규칙대로 L1을 올렸다(사유: "파수꾼 120s+
다운·재기동 소진"). 서비스는 그 직후 전부 정상 복귀했고 밤새 매도 보호·래칫이
정상 작동했다(01시대 WDAY 래칫 실측). 즉 **장애가 아니라 배포 재시작 오인**이다.

절차 (이 커밋의 autodeploy 반영이 끝난 뒤에 실행 — `git log -1`이 이 문서
커밋과 일치하는지 먼저 확인, 재시작과 복구가 겹치지 않게):

```bash
cd /home/ubuntu/Stock-chart-analyze && git log -1 --oneline   # 이 커밋인지 확인
sudo -u ubuntu bash -lc '
  cd /home/ubuntu/Stock-chart-analyze
  set -a; . /home/ubuntu/kis.env; set +a
  .venv/bin/python scripts/kis_l1_readiness.py --broker --scope l0
'
# 결과가 GO일 때만:
sudo -u ubuntu bash -lc '
  cd /home/ubuntu/Stock-chart-analyze
  set -a; . /home/ubuntu/kis.env; set +a
  .venv/bin/python -m bot.kill 0 --lower 배포 재시작 중 파수꾼 오인 상향 — 서비스 정상 실측 후 복구(사용자 승인 2026-08-11)
  .venv/bin/python -m bot.kill
'
```

NO-GO면 하향하지 말고 blockers 원문을 보고한다. 텔레그램 `/진단`으로
`kill-switch: L0` 확인까지가 완료다.

## 1. 근본 수정 — 배포 유예창

### 문제 (24시간 새 3회 실측)

autodeploy.sh가 서비스를 재시작할 때 sentinel 재기동이 120초를 넘기면
watchdog이 heartbeat 공백을 장애로 판정해 L1을 래치한다. L1 하향은 설계상
수동(operator ack CLI)이라, 배포할 때마다 사람이 새벽에 복구하는 악순환이
생겼다(8/10 23:34 실측 — 사용자 L0 복구 1분 뒤 배포 재시작으로 재상향).

### 요구사항

**G1. 유예 마커**: autodeploy.sh가 서비스 재시작 **직전**에 마커 파일
(예: `/opt/stock/deploy_grace.json`, 내용 `{"ts": <epoch>, "sha": "<배포 sha>"}`)
을 원자적으로 쓰고, 재시작 완료 후에는 갱신하지 않는다(삭제 불필요 —
TTL로 만료).

**G2. watchdog 유예 판정**: watchdog은 heartbeat 공백을 카운트하기 전에
마커를 읽어, `now - ts <= DEPLOY_GRACE_S`(기본 **300초**, 환경변수)이면
그 사이클의 재시작 에스컬레이션·L1 상향을 **유예**한다. 유예 중에도
로그는 남긴다("deploy grace 중 — heartbeat age Ns 무시").

**G3. 유예의 안전 한계 (적대 검토 핵심)**:
- 마커가 낡았으면(`now - ts > DEPLOY_GRACE_S`) **완전 무시** — 마커 존재
  자체가 아니라 시각으로만 판정. 지워지지 않은 마커가 워치독을 영구
  무력화하는 일이 없어야 한다.
- `DEPLOY_GRACE_S` 상한 600초 — 그보다 크게 설정돼도 600으로 클램프.
- 마커가 없거나, 파싱 불가·ts 비유한·미래 시각(> now + 60s)이면 유예 없음
  (fail-closed = 워치독 정상 작동).
- 유예가 끝났는데도 heartbeat가 죽어 있으면 **기존 규칙 그대로 즉시**
  에스컬레이션(재시작 시도 → L1). 유예는 카운트다운을 미루는 것이지
  리셋하는 것이 아니다 — 유예 종료 시점의 heartbeat age가 이미 임계 초과면
  다음 사이클에 바로 발동해야 한다.
- kill **상향 이외의 기능**(복구 알림, heartbeat 로그)은 유예와 무관하게 유지.

**G4. 가시성**: 유예 발동/종료를 저널 로그로 남기고, ops_status 스냅샷에
`deploy_grace: true|false` 한 필드만 추가(시크릿 없음).

### 테스트 요구 (최소)

1. 유예 창 내 heartbeat 공백 300s → 재시작 시도 0·L1 상향 0.
2. 마커 ts가 301초 전(만료) + heartbeat 공백 → 기존 동작 그대로 발동.
3. 마커 손상/미래 ts/비유한 → 유예 없음(fail-closed).
4. DEPLOY_GRACE_S=9999 설정 → 600으로 클램프.
5. 유예 종료 직후 사이클에 age가 이미 임계 초과 → 즉시 에스컬레이션.
6. autodeploy.sh가 재시작 직전 마커를 쓰는지(셸 테스트 또는 검증 스크립트).
7. 기존 watchdog 테스트 전체 무손상.

### 주의

- watchdog은 **안전장치의 안전장치**다 — 유예 로직의 어떤 실패도
  "워치독이 안 봄"이 아니라 "유예 없이 정상 감시"로 떨어져야 한다.
- autodeploy.sh는 root로 돈다 — 마커 파일 권한 0644(워치독이 읽기만),
  마커 내용에 시크릿 금지.
- 이 지시서는 kill 정책(상향 규칙·하향 ack)을 바꾸지 않는다. 유예는
  "배포 재시작 소음의 오인"만 제거한다.

## 2. 완료 기준

- §0 복구: readiness GO 증거 + `bot.kill` L0 출력 + `/진단` 캡처.
- §1 구현: 테스트 7종 통과 + compileall + CI, 증거는 실패 테스트명·종료코드
  원문 인용 관례. 구현 후 Claude 적대 검토(P0/P1=0) → 사용자 승인 → 병합.
