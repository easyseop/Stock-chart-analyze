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
   - Variable `BRANCH` = `claude/korean-text-review-o3wmsv`
   - Secret `GH_PAT` = (fine-grained PAT — `github_pat_...` 전체)
   - Secret `TG_TOKEN` = (텔레그램 봇 토큰)
   - Secret `TG_CHAT` = (텔레그램 chat id)
4. **크론 등록**: 워커 → Settings → Triggers → Cron Triggers → Add
   - `*/5 * * * *` (5분마다)
5. **동작 확인**: 워커 URL(`https://fastlane-kicker.<계정>.workers.dev`)을
   브라우저로 열면 현재 판정(JSON)이 보임 — `market.open`과
   `heartbeat_age_min`이 나오면 연결 성공. (URL 조회는 발사하지 않음 — 안전)

## 판정 로직 (worker.js 상단 상수)

| 하트비트 나이 | 행동 |
|---|---|
| <12분 | 아무것도 안 함 |
| 12~30분 | fast 발사(조용히 — 정상 슬롯 보정) |
| 30~45분 | fast 발사 + ⚠️ 텔레그램 |
| 45분+ | fast 발사 + 🚨 텔레그램 |
| dispatch 실패/런 미생성 | 🚨 즉시(PAT 만료·GitHub 이벤트 이상) |

## 보안 메모
- PAT는 Actions RW·이 repo 한정·90일 만료 — 유출 시 피해 상한은 '빌드 강제
  실행'이고, guard가 신선하면 no-op이라 실질 피해는 공회전.
- PAT가 이제 cron-job.org와 CF 두 곳에 있음 — 90일 갱신 때 **두 곳 모두** 교체.
- 실거래 전 승격(B5): 이 워커에 파수꾼 하트비트 감시를 추가(계획 문서 참조).
