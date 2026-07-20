/**
 * 매매 차선 발사·검증 워커 (RELIABILITY A2)
 *
 * GitHub schedule과 독립된 외부 계층 — "정시 보장"이 아니라, 5분마다 깨어나
 * ① 장중인지 ② 마지막 실빌드(state 하트비트)가 얼마나 낡았는지 확인하고,
 * 낡았으면 fast 차선을 발사(workflow_dispatch)한 뒤 ③ 런이 '실제 생성'됐는지
 * 검증, 단계별로 텔레그램 경보를 낸다(F1·F2·F7 대응).
 *
 *   나이 < 14분          → 아무것도 안 함(신선). 단 배포 SHA가 HEAD보다 뒤면 재배포(B7)
 *   14~30분              → fast 발사
 *   30~45분              → fast 발사 + ⚠️ P1 경보
 *   45분+                → fast 발사 + 🚨 P0 경보
 *   발사했는데 60초 내 런 미생성 → 🚨 P0 (GitHub 이벤트 계층 이상 — 실측 B6 유형)
 *   워커가 4틱(≈20분) 연속 유일 발사원 → 🚨 P0 (GitHub 크론 사망 추정)
 *
 * 하트비트는 **GitHub contents API**로 읽는다(raw.githubusercontent CDN 엣지
 * 캐시가 낡은 사본을 줘 워커가 헛발사/오판하던 C1 사고를 원천 차단).
 *
 * 설정(대시보드 → Settings):
 *   [Variables]  REPO="easyseop/Stock-chart-analyze"
 *                BRANCH="claude/happy-gauss-cwoq21"   // 기본 브랜치(2026-07-20 교체)
 *   [Secrets]    GH_PAT   — fine-grained PAT(Actions RW + Contents R, 이 repo만)
 *                TG_TOKEN — 텔레그램 봇 토큰
 *                TG_CHAT  — 텔레그램 chat id
 *   [Trigger]    Cron: *\/5 * * * *   (5분마다)
 *
 * 사용량: 하루 ~600 GitHub API 요청 — PAT 한도(5,000/시)의 극히 일부.
 */

const FRESH_MIN = 14;   // C1: raw CDN 여유 감안 12→14(guard 12분보다 살짝 크게)
const P1_MIN = 30;
const P0_MIN = 45;
const SOLE_LAUNCHER_TICKS = 4;   // 이만큼 연속 발사 = GitHub 크론이 죽고 워커가 유일 발사원

// 베스트에포트 크로스틱 상태 — 웜 아이솔레이트 동안만 유지(재시작 시 리셋돼도 무해).
let _fireStreak = 0;         // 연속으로 fast를 발사한 틱 수(신선 틱에서 0으로 리셋)
let _lastRedeploySha = null; // 같은 HEAD로 재배포를 반복 발사하지 않게(B7 억제)

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(tick(env));
  },
  // 수동 점검용: 브라우저로 워커 URL 열면 현재 판정을 보여준다(발사는 안 함)
  async fetch(req, env) {
    const s = await status(env);
    return new Response(JSON.stringify(s, null, 1),
                        { headers: { "content-type": "application/json" } });
  },
};

function marketOpen(now = new Date()) {
  // KR: 평일 09:00~15:30 KST(=00:00~06:30 UTC, 이 시간대엔 요일도 UTC=KST)
  const dowUtc = now.getUTCDay();
  const mUtc = now.getUTCHours() * 60 + now.getUTCMinutes();
  const kr = dowUtc >= 1 && dowUtc <= 5 && mUtc >= 0 && mUtc <= 390;
  // US: 미 동부시간 평일 09:30~16:00 (서머타임은 타임존 변환이 처리)
  const et = new Date(now.toLocaleString("en-US", { timeZone: "America/New_York" }));
  const mEt = et.getHours() * 60 + et.getMinutes();
  const us = et.getDay() >= 1 && et.getDay() <= 5 && mEt >= 570 && mEt < 960;
  return { kr, us, open: kr || us };
}

const short = (sha) => (sha || "").slice(0, 7);

async function gh(env, path, init = {}) {
  return fetch(`https://api.github.com/repos/${env.REPO}${path}`, {
    ...init,
    headers: {
      "authorization": `Bearer ${env.GH_PAT}`,
      "accept": "application/vnd.github+json",
      "user-agent": "fastlane-worker",
      ...(init.headers || {}),
    },
  });
}

// 하트비트 → {ageMin, sha, src}. 1순위 contents API(CDN 미경유, C1 방지),
//   실패(PAT에 Contents:Read 없음 등) 시 raw CDN 폴백 → 권한 미갱신이어도 워커가
//   절대 무력화되지 않는다(폴백은 엣지 캐시로 낡을 수 있는 최후 수단). 둘 다 실패=null.
async function heartbeat(env) {
  try {
    const r = await gh(env, `/contents/feed/heartbeat.json?ref=state&cb=${Date.now()}`);
    if (r.ok) {
      const j = await r.json();
      const hb = JSON.parse(atob((j.content || "").replace(/\s/g, "")));
      const t = Date.parse(hb.generated_at);
      if (t) return { ageMin: (Date.now() - t) / 60000, sha: hb.sha || null, src: "api" };
    }
  } catch (e) { /* CDN 폴백으로 */ }
  try {
    const r = await fetch(
      `https://raw.githubusercontent.com/${env.REPO}/state/feed/heartbeat.json?cb=${Date.now()}`,
      { cf: { cacheTtl: 0 }, headers: { "cache-control": "no-cache" } });
    if (r.ok) {
      const hb = await r.json();
      const t = Date.parse(hb.generated_at);
      if (t) return { ageMin: (Date.now() - t) / 60000, sha: hb.sha || null, src: "cdn" };
    }
  } catch (e) { /* null */ }
  return null;
}

// 기본 브랜치 HEAD의 커밋 SHA — 배포된 SHA와 대조(B7). 실패 시 null.
async function headSha(env) {
  const r = await gh(env, `/commits/${env.BRANCH}?cb=${Date.now()}`);
  if (!r.ok) return null;
  try {
    return (await r.json()).sha || null;
  } catch (e) {
    return null;
  }
}

async function tg(env, text) {
  if (!env.TG_TOKEN || !env.TG_CHAT) return;
  await fetch(`https://api.telegram.org/bot${env.TG_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: env.TG_CHAT, text, parse_mode: "HTML" }),
  }).catch(() => {});
}

// ntfy.sh 이중 발행 — NTFY_TOPIC 설정 시에만. 헤더는 ASCII, 본문은 UTF-8(이모지 OK).
//   워커는 GitHub 밖 호스트라, GitHub+텔레그램 동시 장애에도 이 채널은 도달한다.
async function ntfy(env, text) {
  if (!env.NTFY_TOPIC) return;
  const base = (env.NTFY_SERVER || "https://ntfy.sh").replace(/\/$/, "");
  await fetch(`${base}/${env.NTFY_TOPIC}`, {
    method: "POST",
    headers: { "Title": "stockbot P0", "Priority": "urgent", "Tags": "rotating_light" },
    body: text.replace(/<[^>]+>/g, ""),   // 텔레그램 HTML 태그 제거
  }).catch(() => {});
}

// P0(치명) — 텔레그램 + ntfy 동시(단일 채널 장애 방어). P1(⚠️)은 tg만.
async function p0(env, text) {
  await tg(env, text);
  await ntfy(env, text);
}

async function status(env) {
  const mkt = marketOpen();
  const hb = await heartbeat(env);
  const head = await headSha(env);
  return {
    market: mkt,
    heartbeat_age_min: hb === null ? null : Math.round(hb.ageMin),
    heartbeat_src: hb === null ? null : hb.src,   // "api"(정상) / "cdn"(폴백=PAT 권한 확인)
    deployed_sha: hb === null ? null : short(hb.sha),
    head_sha: short(head),
    sha_drift: hb && head ? hb.sha !== head : null,
    fire_streak: _fireStreak,
  };
}

async function dispatch(env, update) {
  return gh(env, "/actions/workflows/daily.yml/dispatches", {
    method: "POST",
    body: JSON.stringify({ ref: env.BRANCH, inputs: { update } }),
  });
}

async function tick(env) {
  const mkt = marketOpen();
  if (!mkt.open) { _fireStreak = 0; return; }   // 장외 — 아무것도 안 함
  const hb = await heartbeat(env);
  if (hb === null) return;                       // 하트비트 조회 실패 — 다음 틱에
  const age = hb.ageMin;

  if (age < FRESH_MIN) {
    _fireStreak = 0;                             // 신선 = 정상 발사원(GitHub 크론) 작동 중
    // ── B7: 배포 SHA가 HEAD보다 뒤면 재배포 유도 ──────────────────────
    //   하트비트는 신선한데 코드가 낡음 = 빌드는 도는데 옛 코드 배포 중(머지가
    //   push 빌드를 안 띄운 B7 시나리오). 재배포 후 하트비트 sha가 HEAD로 바뀌면
    //   자동 종료. 같은 HEAD 반복 발사는 _lastRedeploySha로 억제(베스트에포트).
    if (hb.sha) {
      const head = await headSha(env);
      if (head && hb.sha !== head && _lastRedeploySha !== head) {
        _lastRedeploySha = head;
        const d = await dispatch(env, "none");   // 재배포만(데이터 그대로, ~2분)
        if (d.status === 204) {
          await tg(env, `⚠️ 배포 SHA 뒤처짐 ${short(hb.sha)}→${short(head)} — 재배포 발사(B7: 머지가 빌드 미발동 추정).`);
        } else {
          await p0(env, `🚨 재배포 발사 실패 HTTP ${d.status} — PAT 만료/권한 확인.`);
        }
      }
    }
    return;                                       // 신선하면 fast 발사는 불필요
  }

  // ── 정체(≥14분) → 발사 ──────────────────────────────────────────────
  //   한국 장중엔 fast(미국 편향, 국내 미스캔) 대신 kr 발사 — --update-kr +
  //   스크리너(국내 포함)로 국내 신호까지 생성. 한/미 장은 겹치지 않아 단순 분기.
  //   (kr 캐시 시딩은 야간 full 크론/수동 full이 담당 — 워커는 갱신 유지)
  const mode = mkt.kr ? "kr" : "fast";
  const d = await dispatch(env, mode);            // guard가 신선도 재확인(중복 발사 무해)
  _fireStreak++;

  // 발사 검증(F7·B6) — 60초 뒤 dispatch 런이 '실제 생성'됐는지 확인
  let created = null;
  if (d.status === 204) {
    await new Promise((res) => setTimeout(res, 60000));
    const r = await gh(env,
      "/actions/runs?event=workflow_dispatch&per_page=5&created=>" +
      new Date(Date.now() - 5 * 60000).toISOString());
    if (r.ok) {
      const j = await r.json();
      created = (j.workflow_runs || []).length > 0;
    }
  }

  const a = Math.round(age);
  const sole = _fireStreak >= SOLE_LAUNCHER_TICKS;
  const soleNote = sole ? ` · 워커가 ${_fireStreak}틱 연속 유일 발사원(GitHub 크론 사망 추정)` : "";
  if (d.status !== 204) {
    await p0(env, `🚨 <b>발사 실패</b> — dispatch HTTP ${d.status} (하트비트 ${a}분). PAT 만료/권한 확인 필요 — 외부 킥(cron-job+워커) 양쪽 사망 가능.`);
  } else if (created === false) {
    await p0(env, `🚨 <b>런 미생성</b> — dispatch 204인데 60초 내 런이 안 생김 (하트비트 ${a}분). GitHub 이벤트 계층 이상 추정(B6 유형).`);
  } else if (age >= P0_MIN || sole) {
    await p0(env, `🚨 <b>매매 차선 정체 ${a}분</b> — fast 재발사함${soleNote}. 지속되면 GitHub Actions 상태 확인 필요.`);
  } else if (age >= P1_MIN) {
    await tg(env, `⚠️ 매매 차선 ${a}분 정체 — fast 재발사함(자동 복구 중).`);
  }
  // 14~30분 구간은 조용히 발사만(정상 슬롯 보정 — 알림 피로 방지)
}
