# Cloudflare Worker 설정 가이드 — 매매 차선 발사·검증 (~20분, 무료)

> 역할: 5분마다 깨어나 **장중 + 매매 피드가 12분+ 낡음**이면 fast 차선을
> 발사하고, 런이 실제 생성됐는지 검증, 정체 단계별로 텔레그램 경보.
> GitHub 크론(드랍률 88% 실측)·cron-job.org(발사만, 검증 없음)와 독립된 3중 계층.

## 준비물
- Cloudflare 계정(무료) — https://dash.cloudflare.com 가입
- 기존 fine-grained PAT(cron-job.org에 넣은 것과 **같은 토큰** 재사용 가능)
- 텔레그램 봇 토큰·chat id(이미 GitHub Secrets에 있는 것과 동일 값)

## 단계 (대시보드 방식 — 코드 복붙)

1. **Workers 생성**: 대시보드 → Workers & Pages → Create → "Create Worker"
   → 이름 `fastlane-kicker` → Deploy (기본 hello world로 일단 배포)
2. **코드 교체**: 방금 만든 워커 → "Edit code" → 전체 지우고
   이 폴더의 `worker.js` 내용 붙여넣기 → Deploy
3. **변수 설정**: 워커 → Settings → Variables and Secrets
   - Variable `REPO` = `easyseop/Stock-chart-analyze`
   - Variable `BRANCH` = `claude/happy-gauss-cwoq21`   (2026-07-20 기본 브랜치 교체)
   - Secret `GH_PAT` = (fine-grained PAT — `github_pat_...` 전체)
   - Secret `TG_TOKEN` = (텔레그램 봇 토큰)
   - Secret `TG_CHAT` = (텔레그램 chat id)
   - (선택) Secret `NTFY_TOPIC` = (텔레그램과 별개 P0 이중 채널 쓸 때 — §OPERATIONS 7.1)
4. **크론 등록**: 워커 → Settings → Triggers → Cron Triggers → Add
   - `*/5 * * * *` (5분마다)
5. **동작 확인**: 워커 URL(`https://fastlane-kicker.<계정>.workers.dev`)을
   브라우저로 열면 현재 판정(JSON)이 보임 — `market.open`과
   `heartbeat_age_min`이 나오면 연결 성공. (URL 조회는 발사하지 않음 — 안전)

## 판정 로직 (worker.js 상단 상수)

| 하트비트 나이 | 행동 |
|---|---|
| <14분 | 발사 안 함. 단 **배포 SHA≠HEAD면 재배포 발사(B7)** |
| 14~30분 | fast 발사(조용히 — 정상 슬롯 보정) |
| 30~45분 | fast 발사 + ⚠️ 텔레그램 |
| 45분+ | fast 발사 + 🚨 텔레그램+ntfy |
| dispatch 실패/런 미생성 | 🚨 즉시(PAT 만료·GitHub 이벤트 이상) |
| 4틱(≈20분) 연속 유일 발사원 | 🚨 GitHub 크론 사망 추정 |

## 이번 개정(감사 rank2·5·7)
- **하트비트를 GitHub contents API로 읽음**(raw CDN 엣지 캐시가 낡은 사본을 줘
  헛발사하던 C1 차단). 실패 시 raw CDN 폴백이라 **워커가 무력화되지 않음**.
  → 정상 동작하려면 PAT에 **Contents: Read** 권한 필요(없어도 CDN 폴백으로 동작하나
    브라우저로 워커 URL 열어 `heartbeat_src`가 `"cdn"`이면 권한 추가 권장).
- **배포 SHA 자가치유(B7)**: 머지가 push 빌드를 안 띄워도(App 토큰 머지) HEAD와
  대조해 재배포를 발사. `daily.yml` 하트비트에 `sha`가 실린 뒤부터 작동.
- **유일 발사원 P0(#7)**: 워커만 4틱 연속 발사 중이면 GitHub 크론 사망으로 보고 P0.
- **P0 ntfy 이중화**: `NTFY_TOPIC` 설정 시 🚨 알림을 텔레그램+ntfy 동시 —
  워커는 GitHub 밖이라 GitHub+텔레그램 동시 장애에도 이 경보는 도달.

## 보안 메모
- PAT는 Actions RW + Contents R·이 repo 한정·90일 만료 — 유출 시 피해 상한은
  '빌드 강제 실행'이고, guard가 신선하면 no-op이라 실질 피해는 공회전.
- PAT가 cron-job.org와 CF 두 곳에 있음 — 90일 갱신 때 **두 곳 모두** 교체.
- 실거래 전 승격(B5): 이 워커에 파수꾼 하트비트 감시를 추가(계획 문서 참조).
