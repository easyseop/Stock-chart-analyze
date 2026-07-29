"use strict";

const {
  optionalNumber,
  positionInvestmentSummary,
  holdingPeriod,
  positionDistances,
  strategyStats,
  concentrationRows,
  maximumDrawdown,
  completeHoldingsValue,
  completeHoldingsSeries,
} = globalThis.PortfolioMath;

const API = Object.freeze({
  signals: "../api/signals.json",
  paper: "../api/paper_auto.json",
  track: "../api/track.json",
  portfolio: "../api/portfolio.json",
  chart: "../api/chart.json",
  quotes: "../api/quotes.json",
  performance: "../api/performance.json",
  trades: "../api/trades.json",
  postExit: "../api/post-exit.json",
});

const VIEW_META = Object.freeze({
  briefing: { eyebrow: "TODAY · DECISION BRIEF", title: "오늘 브리핑" },
  now: { eyebrow: "STRATEGY A · REVERSAL", title: "전략 A · 진입 후보", group: "now" },
  watch: { eyebrow: "STRATEGY A · WATCH", title: "전략 A · 관찰 후보", group: "watch" },
  shelf: { eyebrow: "STRATEGY B", title: "매물대 반등 후보", group: "shelf" },
  portfolio: { eyebrow: "PRIVATE · READ ONLY", title: "내 자산" },
  performance: { eyebrow: "KIS vs MARKET", title: "성과 · 지수 비교" },
});

const state = {
  view: "briefing",
  market: "all",
  query: "",
  sort: "stage",
  signalsDoc: null,
  paper: null,
  track: null,
  portfolio: null,
  performance: null,
  trades: null,
  postExit: null,
  performanceMarket: "US",
  performanceRange: "today",
  performanceHidden: new Set(),
  shelfMode: "entry",
  portfolioMode: "holdings",
  tradeSleeve: "all",
  tradeSide: "all",
  tradeOutcome: "all",
  postExitHorizon: "5",
  postExitQuality: "all",
  publicError: null,
  portfolioError: null,
  performanceError: null,
  tradesError: null,
  postExitError: null,
  loading: true,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const content = $("#content");
const dialog = $("#detail-dialog");
let portfolioTimer = null;
const priceChartHidden = {
  live: new Set(),
  daily: new Set(),
};

function escapeHTML(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function finite(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function formatPrice(value, ccy = "USD", signed = false) {
  const number = finite(value);
  const sign = signed ? (number >= 0 ? "+" : "−") : (number < 0 ? "−" : "");
  const amount = Math.abs(number);
  if (ccy === "KRW") return `${sign}${Math.round(amount).toLocaleString("ko-KR")}원`;
  return `${sign}$${amount.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function formatPercent(value, digits = 1) {
  const number = finite(value);
  return `${number >= 0 ? "+" : ""}${number.toFixed(digits)}%`;
}

function formatOptionalPrice(value, ccy = "USD", signed = false) {
  const number = optionalNumber(value);
  return number === null ? "—" : formatPrice(number, ccy, signed);
}

function formatOptionalPercent(value, digits = 1) {
  const number = optionalNumber(value);
  return number === null ? "—" : formatPercent(number, digits);
}

function formatOpenedDate(value) {
  const period = holdingPeriod(value);
  if (!period) return null;
  const [year, month, day] = period.opened.split("-");
  return {
    ...period,
    label: `${year}.${month}.${day}`,
  };
}

function marketLabel(ccy) {
  return ccy === "KRW" ? "KR · 원화" : "US · 달러";
}

function groupSignals(group) {
  return (state.signalsDoc?.signals || []).filter((signal) => signal.group === group);
}

function currentSignalGroup() {
  if (state.view === "shelf") {
    return state.shelfMode === "watch" ? "shelf_watch" : "shelf";
  }
  return VIEW_META[state.view]?.group;
}

function relativeMinutes(iso) {
  const timestamp = Date.parse(iso || "");
  if (!Number.isFinite(timestamp)) return null;
  return Math.max(0, Math.floor((Date.now() - timestamp) / 60000));
}

async function fetchJSON(url, timeoutMs = 12000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const separator = url.includes("?") ? "&" : "?";
  try {
    const response = await fetch(`${url}${separator}v=${Date.now()}`, {
      cache: "no-store",
      signal: controller.signal,
      headers: { "Accept": "application/json" },
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

async function loadData({ quiet = false } = {}) {
  if (!quiet) {
    state.loading = true;
    renderLoading();
  }
  const refresh = $("#refresh-button");
  refresh.classList.add("spinning");

  const publicResults = await Promise.allSettled([
    fetchJSON(API.signals),
    fetchJSON(API.paper),
    fetchJSON(API.track),
  ]);
  if (publicResults[0].status === "fulfilled") {
    state.signalsDoc = publicResults[0].value;
    state.publicError = null;
    const note = state.signalsDoc?.note;
    if (note) $("#disclaimer").textContent = note;
  } else {
    state.publicError = publicResults[0].reason;
  }
  if (publicResults[1].status === "fulfilled") state.paper = publicResults[1].value;
  if (publicResults[2].status === "fulfilled") state.track = publicResults[2].value;

  try {
    state.portfolio = await fetchJSON(API.portfolio, 10000);
    state.portfolioError = null;
  } catch (error) {
    state.portfolio = null;
    state.portfolioError = error;
  }
  if (isPrivateDashboard()) {
    try {
      state.performance = await fetchJSON(API.performance, 10000);
      state.performanceError = null;
    } catch (error) {
      state.performance = null;
      state.performanceError = error;
    }
    try {
      state.trades = await fetchJSON(`${API.trades}?limit=200`, 10000);
      state.tradesError = null;
    } catch (error) {
      state.trades = null;
      state.tradesError = error;
    }
    try {
      state.postExit = await fetchJSON(API.postExit, 10000);
      state.postExitError = null;
    } catch (error) {
      state.postExit = null;
      state.postExitError = error;
    }
  }

  state.loading = false;
  refresh.classList.remove("spinning");
  updateFreshness();
  render();
}

function isPrivateDashboard() {
  return ["127.0.0.1", "localhost", "::1"].includes(location.hostname);
}

async function refreshPortfolio() {
  try {
    state.portfolio = await fetchJSON(API.portfolio, 10000);
    state.portfolioError = null;
  } catch (error) {
    state.portfolioError = error;
  }
  try {
    state.performance = await fetchJSON(API.performance, 10000);
    state.performanceError = null;
  } catch (error) {
    state.performanceError = error;
  }
  try {
    state.trades = await fetchJSON(`${API.trades}?limit=200`, 10000);
    state.tradesError = null;
  } catch (error) {
    state.tradesError = error;
  }
  try {
    state.postExit = await fetchJSON(API.postExit, 10000);
    state.postExitError = null;
  } catch (error) {
    state.postExitError = error;
  }
  updateFreshness();
  if (state.view === "briefing") {
    updateHero();
    renderBriefing();
    refreshOpenPortfolioDetail();
  } else if (state.view === "portfolio") {
    updateHero();
    renderPortfolio();
    refreshOpenPortfolioDetail();
  } else if (state.view === "performance") {
    updateHero();
    renderPerformance();
  }
}

function schedulePortfolioRefresh() {
  if (portfolioTimer) clearTimeout(portfolioTimer);
  if (!isPrivateDashboard()) return;
  const seconds = Math.max(5, Math.min(300,
    finite(state.portfolio?.refresh_seconds, 5)));
  portfolioTimer = setTimeout(async () => {
    await refreshPortfolio();
    schedulePortfolioRefresh();
  }, seconds * 1000);
}

function updateFreshness() {
  const freshness = $("#freshness");
  const alert = $("#data-alert");
  alert.className = "alert hidden";

  if ((state.view === "portfolio" || state.view === "briefing") && state.portfolio) {
    const seconds = Number(state.portfolio.price_age_seconds);
    freshness.className = `freshness ${Number.isFinite(seconds) && seconds <= 90 ? "fresh" : "neutral"}`;
    freshness.textContent = Number.isFinite(seconds)
      ? `${Math.max(0, Math.round(seconds))}초 전 시세`
      : "KIS 잔고 조회";
    if (Number.isFinite(seconds) && seconds > 90) {
      alert.className = "alert";
      alert.textContent = `보유종목 시세가 ${Math.round(seconds)}초 동안 갱신되지 않았습니다. 현재가·손익·보호선 거리를 매매 판단에 사용하기 전에 파수꾼 상태를 확인하세요.`;
    }
    return;
  }
  if (state.view === "performance" && state.performance) {
    const minutes = relativeMinutes(state.performance.generated_at);
    freshness.className = `freshness ${minutes !== null && minutes <= 10 ? "fresh" : "neutral"}`;
    freshness.textContent = minutes === null ? "성과 기록 중"
      : minutes === 0 ? "방금 갱신" : `${minutes}분 전 성과`;
    return;
  }
  if (!state.signalsDoc) {
    freshness.className = "freshness error";
    freshness.textContent = "불러오기 실패";
    alert.className = "alert error";
    alert.textContent = "최신 신호를 가져오지 못했습니다. 이전 값을 조용히 보여주지 않고 화면을 중단했습니다.";
    return;
  }
  const minutes = relativeMinutes(state.signalsDoc.generated_at);
  if (minutes === null) {
    freshness.className = "freshness stale";
    freshness.textContent = "갱신 시각 미확인";
    alert.className = "alert";
    alert.textContent = "데이터 생성 시각을 확인할 수 없습니다. 주문 판단에는 사용하지 마세요.";
    return;
  }
  if (minutes >= 15) {
    freshness.className = "freshness stale";
    freshness.textContent = `${minutes}분 전 · 오래됨`;
    alert.className = "alert";
    alert.textContent = `마지막 신호가 ${minutes}분 전에 생성됐습니다. 화면의 가격과 현재 시장 가격이 다를 수 있습니다.`;
  } else {
    freshness.className = "freshness fresh";
    freshness.textContent = minutes === 0 ? "방금 갱신" : `${minutes}분 전 갱신`;
  }
}

function setView(view, { updateHash = true } = {}) {
  if (!VIEW_META[view]) return;
  state.view = view;
  if (updateHash) history.replaceState(null, "", `#${view}`);
  $$(".nav-item, .bottom-item").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
  });
  $("#page-eyebrow").textContent = VIEW_META[view].eyebrow;
  $("#page-title").textContent = VIEW_META[view].title;
  $("#signal-toolbar").classList.toggle("hidden", !VIEW_META[view].group);
  updateFreshness();
  render();
}

function updateHero() {
  const hero = $("#hero");
  const number = $("#hero-number");
  const unit = $("#hero-unit");
  const kicker = $("#hero-kicker");
  const description = $("#hero-description");
  hero.classList.remove("hidden");

  if (state.view === "briefing") {
    const positions = state.portfolio?.positions || [];
    const attention = positions.flatMap(positionAttention);
    const freshSignals = (state.signalsDoc?.signals || []).filter((item) => item.fresh);
    number.textContent = (state.portfolio ? attention.length : freshSignals.length)
      .toLocaleString("ko-KR");
    unit.textContent = state.portfolio ? "확인" : "새 후보";
    kicker.textContent = state.portfolio ? "오늘 먼저 볼 것" : "오늘의 새 신호";
    description.textContent = state.portfolio
      ? attention.length
        ? "보호선과 목표선에 가까운 종목부터 보여드려요. 아래에서 전략과 시장 대비 성과까지 이어서 확인하세요."
        : "현재 보호선에 급하게 가까운 종목은 없습니다. 전략 A·B와 시장 대비 성과를 함께 확인하세요."
      : "공개 화면에서는 새 신호와 모의성과를 요약합니다. KIS 보유자산 브리핑은 Oracle 로컬 화면에서만 보여요.";
  } else if (VIEW_META[state.view].group) {
    const group = currentSignalGroup();
    const items = groupSignals(group);
    number.textContent = items.length.toLocaleString("ko-KR");
    unit.textContent = "종목";
    kicker.textContent = state.view === "now" ? "전략 A · 전환 확인" :
      state.view === "watch" ? "전략 A · 전환 대기" :
        state.shelfMode === "watch" ? "전략 B · 반등 확인 대기" :
          "전략 B · 매물대 반등";
    description.textContent = state.view === "now"
      ? "하락에서 상승으로 전환이 확인된 전략 A의 실제 KIS 진입 후보입니다."
      : state.view === "watch"
        ? "전략 A 후보 중 아직 매수 조건을 완전히 충족하지 않은 관찰 종목입니다."
        : state.shelfMode === "watch"
          ? "매물대 구조는 유효하지만 반등·거래량 확인이 덜 된 관찰 종목입니다. 자동매수 대상이 아닙니다."
          : "전략 B의 매물대 반등 조건을 모두 통과한 진입 후보입니다.";
  } else if (state.view === "portfolio") {
    const positions = state.portfolio?.positions || [];
    const trades = state.trades?.trades || [];
    const postExit = state.postExit?.events || [];
    const historyMode = state.portfolioMode === "history";
    const postExitMode = state.portfolioMode === "post-exit";
    number.textContent = (historyMode ? trades.length
      : postExitMode ? postExit.length : positions.length).toLocaleString("ko-KR");
    unit.textContent = historyMode ? "체결" : postExitMode ? "수익 매도" : "보유";
    kicker.textContent = state.portfolio
      ? historyMode ? "확정 체결 거래이력"
        : postExitMode ? "익절 뒤 추가 상승 사후추적"
          : "KIS 계좌 · 읽기 전용"
      : "내 컴퓨터에서만";
    description.textContent = state.portfolio
      ? historyMode
        ? "평단가·매도가와 환율·수수료를 반영한 실현손익을 로컬 원장에서 확인합니다."
        : postExitMode
          ? "평단 대비 총 상승과 매도가 뒤 놓친 상승을 거래일별로 추적해 청산 규칙의 공통점을 찾습니다."
          : "주문 기능과 분리된 조회 전용 연결입니다. 정보는 이 브라우저 밖으로 공개되지 않아요."
      : "실제 보유 정보는 공개 사이트에 표시하지 않습니다. 로컬 대시보드를 실행하면 이곳에서 확인할 수 있어요.";
  } else {
    const market = state.performance?.markets?.[state.performanceMarket];
    const latest = market?.series?.at(-1);
    const returnValue = latest ? finite(latest.account) : finite(state.paper?.ret_pct);
    number.textContent = formatPercent(returnValue);
    unit.textContent = "";
    kicker.textContent = latest
      ? `${market.label} 봇 운용자산 TWR · 지수와 같은 기준점`
      : "모의투자 누적 수익률";
    description.textContent = latest
      ? "매수·매도 금액 변화는 제거하고 전략 A·B와 시장지수의 실제 성과를 비교합니다."
      : "지수 비교 데이터가 쌓이기 전에는 기존 모의투자 성과를 표시합니다.";
  }
}

function filteredSignals() {
  const group = currentSignalGroup();
  let items = groupSignals(group);
  if (state.market !== "all") items = items.filter((item) => item.ccy === state.market);
  if (state.query) {
    const query = state.query.toLowerCase();
    items = items.filter((item) =>
      String(item.name || "").toLowerCase().includes(query) ||
      String(item.code || "").toLowerCase().includes(query));
  }
  const sorters = {
    stage: (a, b) => finite(b.stage) - finite(a.stage) || finite(b.norm) - finite(a.norm),
    fresh: (a, b) => Number(Boolean(b.fresh)) - Number(Boolean(a.fresh)) || finite(b.stage) - finite(a.stage),
    range: (a, b) => finite(a.range_pos, 1) - finite(b.range_pos, 1),
    name: (a, b) => String(a.name).localeCompare(String(b.name), "ko"),
  };
  return [...items].sort(sorters[state.sort] || sorters.stage);
}

function stageBars(stage) {
  const current = Math.max(0, Math.min(4, Math.round(finite(stage))));
  return Array.from({ length: 4 }, (_, index) => `<i class="${index < current ? "on" : ""}"></i>`).join("");
}

function strategyDefinitionMarkup({ compact = false } = {}) {
  return `<section class="strategy-definition ${compact ? "compact" : ""}" aria-label="전략 A와 B 정의">
    <article>
      <span class="badge strategy-a">전략 A · 전환 확인</span>
      <strong>하락 흐름이 상승으로 돌아서는 것을 확인하고 진입</strong>
      <p>저점권에서 전환 단계·추세·과열·손절폭을 함께 확인합니다. <b>A 관찰</b>은 전환 또는 눌림 조건을 아직 기다리는 종목이라 매수 대상이 아닙니다.</p>
    </article>
    <article>
      <span class="badge shelf">전략 B · 매물대 반등</span>
      <strong>큰 거래량이 쌓인 지지대에서 반등을 확인하고 진입</strong>
      <p>장기 POC·밸류영역, 머리 위 매물, 반등 캔들, 거래량과 손익비를 확인합니다. <b>B 관찰</b>은 지지대에 있지만 반등 확인이 덜 된 종목이며 자동매수에서 제외됩니다.</p>
    </article>
  </section>`;
}

function shelfModeMarkup() {
  if (state.view !== "shelf") return "";
  const entryCount = groupSignals("shelf").length;
  const watchCount = groupSignals("shelf_watch").length;
  return `<div class="subview-tabs" aria-label="전략 B 후보 상태">
    <button type="button" data-shelf-mode="entry"
      class="${state.shelfMode === "entry" ? "active" : ""}">
      진입 후보 <span>${entryCount}</span>
    </button>
    <button type="button" data-shelf-mode="watch"
      class="${state.shelfMode === "watch" ? "active" : ""}">
      B 관찰 <span>${watchCount}</span>
    </button>
  </div>`;
}

function signalCard(signal) {
  const tactic = signal.tactic || {};
  const isShelfWatch = signal.group === "shelf_watch";
  const badges = [
    signal.fresh ? `<span class="badge new">NEW</span>` : "",
    signal.group === "now" ? `<span class="badge strategy-a">전략 A</span>` : "",
    signal.group === "watch" ? `<span class="badge strategy-a">A 관찰</span>` : "",
    tactic.label ? `<span class="badge tactic">${escapeHTML(tactic.label)}</span>` : "",
    signal.group === "shelf" ? `<span class="badge shelf">전략 B</span>` : "",
    isShelfWatch ? `<span class="badge shelf">B 관찰</span>` : "",
  ].join("");
  const entryLabel = isShelfWatch ? "현재" : "진입";
  const stopLabel = isShelfWatch ? "참고 손절" : "손절";
  const targetLabel = isShelfWatch ? "참고 목표" : "목표";
  return `
    <button class="signal-card" type="button" data-signal-id="${escapeHTML(signal.id)}">
      <span class="card-head">
        <span>
          <span class="ticker">${escapeHTML(signal.code)} · ${marketLabel(signal.ccy)}</span>
          <span class="stock-name">${escapeHTML(signal.name)}</span>
        </span>
        <span class="badge-row">${badges}</span>
      </span>
      <span class="price">${formatPrice(signal.price, signal.ccy)}</span>
      <span class="metric-row">
        <span class="metric"><small>${entryLabel}</small><b>${formatOptionalPrice(signal.entry, signal.ccy)}</b></span>
        <span class="metric stop"><small>${stopLabel}</small><b>${formatOptionalPrice(signal.stop, signal.ccy)}</b></span>
        <span class="metric target"><small>${targetLabel}</small><b>${formatOptionalPrice(signal.target, signal.ccy)}</b></span>
      </span>
      ${isShelfWatch ? `<span class="watch-reason">${escapeHTML(signal.shelf?.reason || "반등 확인 대기")}</span>` : `<span class="stage-line">
        <small>단계 ${Math.round(finite(signal.stage))}/4</small>
        <span class="stage-track">${stageBars(signal.stage)}</span>
      </span>`}
    </button>`;
}

function renderSignals() {
  if (state.publicError || !state.signalsDoc) {
    content.innerHTML = errorState("신호를 불러오지 못했어요", "공개 데이터 연결을 확인한 뒤 다시 시도해 주세요.");
    return;
  }
  const items = filteredSignals();
  const intro = `${strategyDefinitionMarkup()}${shelfModeMarkup()}`;
  if (!items.length) {
    const hasGroup = groupSignals(currentSignalGroup()).length > 0;
    content.innerHTML = intro + emptyState(
      hasGroup ? "검색 조건에 맞는 종목이 없어요" : "오늘은 해당 신호가 없어요",
      hasGroup ? "필터나 검색어를 바꾸면 다른 종목을 볼 수 있어요." :
        "신호가 0건인 날도 정상입니다. 조건을 낮추거나 임의 종목을 만들지 않습니다.");
  } else {
    content.innerHTML = `${intro}<div class="signal-grid">${items.map(signalCard).join("")}</div>`;
  }
  $$("[data-shelf-mode]", content).forEach((button) =>
    button.addEventListener("click", () => {
      state.shelfMode = button.dataset.shelfMode;
      updateHero();
      renderSignals();
    }));
  $$(".signal-card", content).forEach((card) => {
    card.addEventListener("click", () => {
      const signal = (state.signalsDoc.signals || []).find((item) => item.id === card.dataset.signalId);
      if (signal) openSignalDetail(signal);
    });
  });
}

function emptyState(title, description) {
  return `<div class="empty-state">
    <span class="state-icon">○</span>
    <h2>${escapeHTML(title)}</h2>
    <p>${escapeHTML(description)}</p>
  </div>`;
}

function errorState(title, description) {
  return `<div class="error-state">
    <span class="state-icon">!</span>
    <h2>${escapeHTML(title)}</h2>
    <p>${escapeHTML(description)}</p>
    <button class="retry-button" type="button" data-retry>다시 불러오기</button>
  </div>`;
}

function openSignalDetail(signal) {
  $("#detail-market").textContent = `${marketLabel(signal.ccy)} · ${escapeHTML(signal.code)}`;
  $("#detail-title").textContent = signal.name || signal.code;
  const range = Math.max(0, Math.min(100, finite(signal.range_pos, .5) * 100));
  const tactic = signal.tactic || {};
  const shelf = signal.shelf;
  const isShelfWatch = signal.group === "shelf_watch";
  $("#detail-content").innerHTML = `
    <div class="detail-price">${formatPrice(signal.price, signal.ccy)}</div>
    <div class="detail-grid">
      <div class="detail-box"><small>${isShelfWatch ? "현재가" : "제안 진입가"}</small><strong>${formatOptionalPrice(signal.entry, signal.ccy)}</strong></div>
      <div class="detail-box"><small>${isShelfWatch ? "참고 손절선" : "손절가"}</small><strong class="loss">${formatOptionalPrice(signal.stop, signal.ccy)}</strong></div>
      <div class="detail-box"><small>${isShelfWatch ? "참고 목표선" : "목표가"}</small><strong class="gain">${formatOptionalPrice(signal.target, signal.ccy)}</strong></div>
    </div>
    <div class="tactic-box">
      <strong>${isShelfWatch ? "B 관찰 · 아직 주문 대상 아님" : escapeHTML(tactic.label || "진입 전술")}</strong>
      <p>${isShelfWatch
        ? escapeHTML(shelf?.reason || "매물대 반등 조건 확인을 기다립니다.")
        : escapeHTML(tactic.desc || "원본 신호에 전술 설명이 없습니다.")}</p>
    </div>
    <div class="range-box">
      <div class="range-labels"><span>52주 저점</span><span>현재 위치 ${Math.round(range)}%</span><span>52주 고점</span></div>
      <div class="range-track">
        <div class="range-fill"></div>
        <span class="range-dot"></span>
      </div>
    </div>
    ${shelf ? shelfDetail(shelf, signal.ccy) : ""}
  `;
  $(".range-fill", $("#detail-content")).style.width = `${range}%`;
  $(".range-dot", $("#detail-content")).style.left = `${range}%`;
  dialog.showModal();
}

function shelfDetail(shelf, ccy) {
  const checks = Object.entries(shelf.checks || {});
  return `<div class="shelf-box">
    <h3>매물대 정보</h3>
    <div class="shelf-values">
      <div><small>POC</small><b>${formatPrice(shelf.poc, ccy)}</b></div>
      <div><small>VAL</small><b>${formatPrice(shelf.val, ccy)}</b></div>
      <div><small>VAH</small><b>${formatPrice(shelf.vah, ccy)}</b></div>
      <div><small>손익비</small><b>${finite(shelf.rr).toFixed(2)}R</b></div>
    </div>
    ${checks.length ? `<div class="condition-list" aria-label="B 반등 조건">
      ${checks.map(([label, passed]) =>
        `<span class="${passed ? "passed" : "waiting"}">${passed ? "✓" : "○"} ${escapeHTML(label)}</span>`).join("")}
    </div>` : ""}
  </div>`;
}

function positionAttention(position) {
  const { stopPct, targetPct } = positionDistances(position);
  const items = [];
  if (!(optionalNumber(position.stop) > 0)) {
    items.push({
      position, tone: "danger", priority: -200,
      title: "보호선 정보 없음",
      detail: "봇 보호 기준을 확인할 수 없습니다. 수동 검토가 필요합니다.",
    });
  }
  if (stopPct !== null && stopPct <= 0) {
    items.push({
      position, tone: "danger", priority: -100 + stopPct,
      title: "손절선 도달",
      detail: `현재가가 보호선보다 ${Math.abs(stopPct).toFixed(1)}% 낮습니다.`,
    });
  } else if (stopPct !== null && stopPct <= 3) {
    items.push({
      position, tone: "warning", priority: stopPct,
      title: "손절선 근접",
      detail: `보호선까지 ${stopPct.toFixed(1)}% 남았습니다.`,
    });
  }
  if (targetPct !== null && targetPct <= 0) {
    items.push({
      position, tone: "success", priority: 10 + Math.abs(targetPct),
      title: "목표가 도달",
      detail: `현재가가 목표선보다 ${Math.abs(targetPct).toFixed(1)}% 높습니다.`,
    });
  } else if (targetPct !== null && targetPct <= 3) {
    items.push({
      position, tone: "success", priority: 20 + targetPct,
      title: "목표가 근접",
      detail: `목표선까지 ${targetPct.toFixed(1)}% 남았습니다.`,
    });
  }
  return items.sort((a, b) => a.priority - b.priority).slice(0, 1);
}

function positionPlanMarkup(position) {
  const { stopPct, targetPct, progress } = positionDistances(position);
  const missingProtection = stopPct === null
    ? `<span class="plan-missing">보호선 정보 없음 · 수동 확인</span>` : "";
  if (stopPct === null && targetPct === null) {
    return missingProtection;
  }
  const stopText = stopPct === null ? "손절선 없음"
    : stopPct <= 0 ? `손절선 ${Math.abs(stopPct).toFixed(1)}% 이탈`
      : `손절까지 ${stopPct.toFixed(1)}%`;
  const targetText = targetPct === null ? "목표선 없음"
    : targetPct <= 0 ? `목표 ${Math.abs(targetPct).toFixed(1)}% 초과`
      : `목표까지 ${targetPct.toFixed(1)}%`;
  return `${missingProtection}<span class="plan-distance">
    <span class="${stopPct !== null && stopPct <= 3 ? "loss" : ""}">${escapeHTML(stopText)}</span>
    <span class="${targetPct !== null && targetPct <= 3 ? "gain" : ""}">${escapeHTML(targetText)}</span>
  </span>
  ${progress === null ? "" : `<span class="plan-track" aria-label="손절선에서 목표선까지 현재 위치">
    <progress max="100" value="${progress.toFixed(1)}">${progress.toFixed(1)}%</progress>
  </span>`}`;
}

function positionPlanDetailMarkup(position) {
  const stop = optionalNumber(position.stop);
  const target = optionalNumber(position.target);
  const current = optionalNumber(position.cur);
  if (!(stop > 0) && !(target > 0)) {
    return `<div class="position-plan-box danger">
      <div><small>매매 계획 기준</small><strong>보호선과 목표선 정보가 없습니다.</strong></div>
      <span class="plan-missing">봇 원장 연결 또는 수동 보호 기준 확인 필요</span>
    </div>`;
  }
  const labels = [
    stop > 0 ? `손절 ${formatPrice(stop, position.ccy)}` : "",
    target > 0 ? `목표 ${formatPrice(target, position.ccy)}` : "",
    current > 0 && stop > 0 && current > stop && target > current
      ? `남은 손익비 ${((target - current) / (current - stop)).toFixed(2)}R` : "",
  ].filter(Boolean).join(" · ");
  return `<div class="position-plan-box">
    <div><small>매매 계획 기준</small><strong>${escapeHTML(labels)}</strong></div>
    ${positionPlanMarkup(position)}
  </div>`;
}

function strategyRateLabels(stats) {
  return Object.entries(stats.markets).map(([ccy, row]) => {
    const rate = row.buy > 0 ? row.pl / row.buy * 100 : 0;
    return `<span class="${rate >= 0 ? "gain" : "loss"}">${ccy === "KRW" ? "한국" : "미국"} ${formatPercent(rate)}</span>`;
  }).join("");
}

function briefingAttentionCard(item) {
  const { position } = item;
  return `<button class="attention-card ${item.tone}" type="button"
      data-open-position="${escapeHTML(position.code)}">
    <span class="attention-icon">${item.tone === "danger" ? "!" : item.tone === "warning" ? "△" : "✓"}</span>
    <span>
      <small>${escapeHTML(item.title)} · ${escapeHTML(position.code)}</small>
      <strong>${escapeHTML(position.name || position.code)}</strong>
      <p>${escapeHTML(item.detail)}</p>
    </span>
    <span class="attention-price">${formatPrice(position.cur, position.ccy)}</span>
  </button>`;
}

function briefingStrategyCard(sleeve, stats) {
  const usToday = state.performance?.markets?.US?.series?.at(-1)?.[sleeve];
  const krToday = state.performance?.markets?.KR?.series?.at(-1)?.[sleeve];
  return `<button class="brief-card strategy-card" type="button"
      data-go-view="${sleeve === "A" ? "now" : "shelf"}"
      ${sleeve === "B" ? 'data-shelf-mode-target="entry"' : ""}>
    <span class="brief-card-top">
      <span class="badge ${sleeve === "B" ? "shelf" : "strategy-a"}">전략 ${sleeve}</span>
      <span>보유 현황</span>
    </span>
    <strong>${stats.count}종목</strong>
    <p>오늘 미국 ${performanceValue(usToday)} · 한국 ${performanceValue(krToday)}</p>
    <span class="strategy-rate-row">${strategyRateLabels(stats) || "<span>현재 보유 없음</span>"}</span>
  </button>`;
}

function briefingMarketCard(market) {
  const doc = state.performance?.markets?.[market] || {};
  const latest = doc.series?.at(-1);
  const indices = doc.indices || [];
  if (!latest) {
    return `<button class="brief-card market-card" type="button" data-go-view="performance">
      <span class="brief-card-top"><b>${escapeHTML(doc.label || market)}</b><span>기록 중</span></span>
      <strong>—</strong><p>장중 첫 비교값을 기다리고 있어요.</p>
    </button>`;
  }
  const account = optionalNumber(latest.account);
  const available = indices.map((name) => ({
    name, value: optionalNumber(latest.indices?.[name]),
  })).filter((row) => row.value !== null);
  const beats = account === null ? 0 : available.filter((row) => account > row.value).length;
  const indexLabel = available.length === 1 ? "지수" : "두 지수";
  const comparison = !available.length || account === null ? "지수 기록 중"
    : beats === available.length ? `${indexLabel}보다 앞섬`
      : beats === 0 ? `${indexLabel}보다 뒤처짐` : "지수별 엇갈림";
  const tone = beats === available.length ? "gain" : beats === 0 ? "loss" : "";
  const indexText = available.map((row) =>
    `${escapeHTML(row.name)} ${performanceValue(row.value)}`).join(" · ");
  return `<button class="brief-card market-card" type="button" data-go-view="performance"
      data-performance-market-target="${escapeHTML(market)}">
    <span class="brief-card-top"><b>${escapeHTML(doc.label || market)}</b><span class="${tone}">${escapeHTML(comparison)}</span></span>
    <strong class="${account !== null && account >= 0 ? "gain" : "loss"}">${performanceValue(account)}</strong>
    <p>${indexText || "지수 값을 기다리고 있어요."}</p>
  </button>`;
}

function briefingSignalCard(view, label, description, groupOverride = null) {
  const shelfMode = groupOverride === "shelf_watch" ? "watch"
    : groupOverride === "shelf" ? "entry" : "";
  const modeAttribute = shelfMode ? ` data-shelf-mode-target="${shelfMode}"` : "";
  if (!state.signalsDoc) {
    return `<button class="brief-card signal-brief-card" type="button" data-go-view="${view}"${modeAttribute}>
      <span class="brief-card-top"><b>${escapeHTML(label)}</b><span class="loss">연결 확인</span></span>
      <strong>—</strong><p>신호 데이터를 불러오지 못했습니다.</p>
    </button>`;
  }
  const group = groupOverride || VIEW_META[view].group;
  const rows = groupSignals(group);
  const fresh = rows.filter((row) => row.fresh).length;
  const status = group === "shelf_watch" ? "확인 대기"
    : fresh ? `NEW ${fresh}` : "새 신호 없음";
  return `<button class="brief-card signal-brief-card" type="button" data-go-view="${view}"${modeAttribute}>
    <span class="brief-card-top"><b>${escapeHTML(label)}</b><span>${status}</span></span>
    <strong>${rows.length}종목</strong>
    <p>${escapeHTML(description)}</p>
  </button>`;
}

function renderBriefing() {
  const positions = state.portfolio?.positions || [];
  const attention = positions.flatMap(positionAttention)
    .sort((a, b) => a.priority - b.priority);
  const strategies = strategyStats(positions);
  const concentrations = concentrationRows(positions);
  const privateSection = state.portfolio ? `
    <section class="brief-section">
      <div class="section-head">
        <div><span>1</span><h2>지금 확인할 것</h2></div>
        <button type="button" data-go-view="portfolio">내 자산 전체 보기 →</button>
      </div>
      ${attention.length ? `<div class="attention-list">${attention.map(briefingAttentionCard).join("")}</div>` :
        `<div class="calm-card"><span>✓</span><div><strong>급하게 보호선을 확인할 종목이 없어요</strong><p>손절선 또는 목표선 3% 안에 들어온 종목이 생기면 여기에 먼저 표시됩니다.</p></div></div>`}
    </section>
    <section class="brief-section">
      <div class="section-head"><div><span>2</span><h2>전략 A와 B</h2></div><p>금액 대신 시장별 수익률로 비교</p></div>
      <div class="brief-grid strategy-brief-grid">
        ${briefingStrategyCard("A", strategies.A)}
        ${briefingStrategyCard("B", strategies.B)}
        <div class="brief-card concentration-card">
          <span class="brief-card-top"><b>시장별 집중도</b><span>한 종목 비중</span></span>
          ${concentrations.length ? concentrations.map((row) =>
            `<div class="concentration-row"><span>${row.ccy === "KRW" ? "한국" : "미국"} · ${escapeHTML(row.name)}</span><strong class="${row.tone}">${row.weight.toFixed(0)}%</strong></div>`
          ).join("") : "<p>현재 보유 없음</p>"}
          <p>통화가 다른 자산은 억지로 합산하지 않습니다.</p>
        </div>
      </div>
      ${strategyDefinitionMarkup({ compact: true })}
    </section>
    <section class="brief-section">
      <div class="section-head"><div><span>3</span><h2>시장보다 잘하고 있나</h2></div><button type="button" data-go-view="performance">자세히 비교 →</button></div>
      <div class="brief-grid market-brief-grid">
        ${briefingMarketCard("US")}
        ${briefingMarketCard("KR")}
      </div>
    </section>` : `
    <div class="local-gate briefing-gate">
      <span class="state-icon">⌂</span>
      <h2>KIS 보유종목 브리핑은 Oracle 화면에서만 보여요</h2>
      <p>공개 사이트에는 계좌와 보유종목을 보내지 않습니다. 아래 공개 신호와 모의성과는 그대로 확인할 수 있어요.</p>
    </div>`;
  content.innerHTML = `
    ${privateSection}
    <section class="brief-section">
      <div class="section-head"><div><span>${state.portfolio ? "4" : "1"}</span><h2>오늘의 새 후보</h2></div><p>조건을 낮추지 않은 실제 신호 수</p></div>
      <div class="brief-grid signal-brief-grid">
        ${briefingSignalCard("now", "전략 A", "전환이 확인된 진입 후보")}
        ${briefingSignalCard("watch", "A 관찰", "조건 충족을 기다리는 후보")}
        ${briefingSignalCard("shelf", "전략 B", "매물대 반등 진입 후보", "shelf")}
        ${briefingSignalCard("shelf", "B 관찰", "반등·거래량 확인을 기다리는 후보", "shelf_watch")}
      </div>
    </section>`;
  $$("[data-go-view]", content).forEach((button) =>
    button.addEventListener("click", () => {
      if (button.dataset.performanceMarketTarget) {
        state.performanceMarket = button.dataset.performanceMarketTarget;
      }
      if (button.dataset.shelfModeTarget) {
        state.shelfMode = button.dataset.shelfModeTarget;
      }
      setView(button.dataset.goView);
    }));
  $$("[data-open-position]", content).forEach((button) =>
    button.addEventListener("click", () => {
      const position = positions.find((row) => row.code === button.dataset.openPosition);
      if (position) openPortfolioDetail(position);
    }));
}

function portfolioTotals(positions) {
  const totals = {};
  positions.forEach((position) => {
    const metrics = positionInvestmentSummary(position);
    const row = totals[position.ccy] ||= { eval: 0, buy: 0, pl: 0 };
    row.eval += finite(metrics.currentValue);
    row.buy += finite(metrics.investedAmount);
    row.pl += finite(metrics.pnlAmount);
  });
  return totals;
}

function portfolioCard(position) {
  const metrics = positionInvestmentSummary(position);
  const opened = formatOpenedDate(position.opened);
  const tone = metrics.pnlAmount === null
    ? "" : metrics.pnlAmount >= 0 ? "gain" : "loss";
  const sleeve = String(position.sleeve || "A").toUpperCase() === "B" ? "B" : "A";
  const quantity = metrics.quantity === null
    ? "수량 미확인" : `${metrics.quantity.toLocaleString("ko-KR")}주`;
  const holding = opened
    ? `${opened.label} 매수 · ${opened.holdingDays}일째`
    : "매수일 미확인";
  return `
    <button class="portfolio-card" type="button" data-position="${escapeHTML(position.code)}">
      <span class="card-head">
        <span>
          <span class="ticker">${escapeHTML(position.code)} · ${marketLabel(position.ccy)}</span>
          <span class="stock-name">${escapeHTML(position.name)}</span>
        </span>
        <span class="badge ${sleeve === "B" ? "shelf" : "strategy-a"}">전략 ${sleeve}</span>
      </span>
      <span class="portfolio-value">
        <span class="portfolio-current">
          <small>현재가</small>
          <strong>${formatOptionalPrice(metrics.currentPrice, position.ccy)}</strong>
        </span>
        <span class="portfolio-return ${tone}">
          <strong>${formatOptionalPercent(metrics.returnPct)}</strong>
          <small>${formatOptionalPrice(metrics.pnlAmount, position.ccy, true)}</small>
        </span>
      </span>
      <span class="portfolio-price-grid">
        <span><small>내 평균매수가</small><b>${formatOptionalPrice(metrics.averagePrice, position.ccy)}</b></span>
        <span><small>1주당 손익</small><b class="${tone}">${formatOptionalPrice(metrics.perSharePnl, position.ccy, true)}</b></span>
        <span><small>투입금</small><b>${formatOptionalPrice(metrics.investedAmount, position.ccy)}</b></span>
        <span><small>현재 평가금</small><b>${formatOptionalPrice(metrics.currentValue, position.ccy)}</b></span>
      </span>
      <span class="portfolio-meta portfolio-holding">
        <span>${escapeHTML(quantity)}</span>
        <span class="${opened ? "" : "unknown"}">${escapeHTML(holding)}</span>
      </span>
      ${positionPlanMarkup(position)}
    </button>`;
}

function portfolioTabsMarkup(positions) {
  const tradeCount = (state.trades?.trades || []).length;
  const postExitCount = (state.postExit?.events || []).length;
  return `<div class="subview-tabs portfolio-tabs" aria-label="내 자산 보기">
    <button type="button" data-portfolio-mode="holdings"
      class="${state.portfolioMode === "holdings" ? "active" : ""}">
      보유종목 <span>${positions.length}</span>
    </button>
    <button type="button" data-portfolio-mode="history"
      class="${state.portfolioMode === "history" ? "active" : ""}">
      거래이력 <span>${tradeCount}</span>
    </button>
    <button type="button" data-portfolio-mode="post-exit"
      class="${state.portfolioMode === "post-exit" ? "active" : ""}">
      익절 사후추적 <span>${postExitCount}</span>
    </button>
  </div>`;
}

function filteredTrades() {
  let rows = [...(state.trades?.trades || [])];
  if (state.tradeSleeve !== "all") {
    rows = rows.filter((row) => row.sleeve === state.tradeSleeve);
  }
  if (state.tradeSide !== "all") {
    rows = rows.filter((row) =>
      (row.side || "sell").toLowerCase() === state.tradeSide);
  }
  const sells = (row) => (row.side || "sell").toLowerCase() === "sell";
  if (state.tradeOutcome === "stop") {
    rows = rows.filter((row) => sells(row) && row.reason_kind === "stop");
  } else if (state.tradeOutcome === "take_profit") {
    rows = rows.filter((row) =>
      sells(row) && ["take_profit", "trail"].includes(row.reason_kind));
  } else if (state.tradeOutcome === "win") {
    rows = rows.filter((row) =>
      sells(row) && optionalNumber(row.realized_pnl_krw) > 0);
  } else if (state.tradeOutcome === "loss") {
    rows = rows.filter((row) =>
      sells(row) && optionalNumber(row.realized_pnl_krw) < 0);
  }
  return rows;
}

function tradeDateLabel(value) {
  const date = new Date(value || "");
  if (!Number.isFinite(date.getTime())) return "시각 미확인";
  return date.toLocaleString("ko-KR", {
    timeZone: "Asia/Seoul",
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
  });
}

function tradeReasonLabel(kind) {
  return ({
    buy: "매수",
    stop: "손절", take_profit: "익절", time_stop: "타임스탑",
    trail: "트레일", other: "기타 매도",
  })[kind] || "매도";
}

function tradeHistorySummary(rows) {
  const buyRows = rows.filter((row) =>
    (row.side || "sell").toLowerCase() === "buy");
  const sellRows = rows.filter((row) =>
    (row.side || "sell").toLowerCase() === "sell");
  const exact = sellRows.filter((row) =>
    optionalNumber(row.realized_pnl_krw) !== null);
  const wins = exact.filter((row) => finite(row.realized_pnl_krw) > 0).length;
  const losses = exact.filter((row) => finite(row.realized_pnl_krw) < 0).length;
  const decided = wins + losses;
  const pnl = exact.reduce((sum, row) => sum + finite(row.realized_pnl_krw), 0);
  const returns = sellRows.map((row) => optionalNumber(row.return_pct))
    .filter((value) => value !== null);
  const average = returns.length
    ? returns.reduce((sum, value) => sum + value, 0) / returns.length : null;
  return `
    <div class="trade-summary">
      <div class="summary-card"><small>확정 체결</small><strong>${rows.length}건</strong><div>매수 ${buyRows.length} · 매도 ${sellRows.length}</div></div>
      <div class="summary-card"><small>승률</small><strong>${decided ? `${(wins / decided * 100).toFixed(1)}%` : "—"}</strong><div>${wins}승 · ${losses}패</div></div>
      <div class="summary-card"><small>실현손익</small><strong class="${pnl >= 0 ? "gain" : "loss"}">${exact.length ? formatPrice(pnl, "KRW", true) : "—"}</strong><div>환율·수수료 반영</div></div>
      <div class="summary-card"><small>평균 수익률</small><strong class="${finite(average) >= 0 ? "gain" : "loss"}">${average === null ? "—" : formatPercent(average)}</strong><div>표시된 체결 기준</div></div>
    </div>`;
}

function tradeHistoryCard(row) {
  const side = (row.side || "sell").toLowerCase();
  const estimatedPrice = row.price_estimated === true;
  if (side === "buy") {
    const amountNative = optionalNumber(row.amount_native);
    const amountKrw = optionalNumber(row.amount_krw);
    const fillPrice = optionalNumber(row.fill_price ?? row.entry_price);
    const averageAfter = optionalNumber(row.average_price_after);
    const positionAfter = optionalNumber(row.position_qty_after);
    const reason = tradeReasonLabel(row.reason_kind || "buy");
    return `<article class="trade-history-card trade-buy-card">
      <div class="trade-history-head">
        <div>
          <small>${escapeHTML(tradeDateLabel(row.executed_at))} · ${estimatedPrice ? "장부 복원 가격" : "실제 체결 확인"}</small>
          <strong>${escapeHTML(row.name || row.code)} <span>${escapeHTML(row.code)}</span></strong>
        </div>
        <div class="badge-row">
          <span class="badge ${row.sleeve === "B" ? "shelf" : "strategy-a"}">전략 ${row.sleeve === "B" ? "B" : "A"}</span>
          <span class="badge trade-side buy">${escapeHTML(reason)}</span>
        </div>
      </div>
      <div class="trade-price-flow">
        <div><small>${estimatedPrice ? "복원 매수가" : "실제 매수가"}</small><strong>${formatOptionalPrice(fillPrice, row.ccy)}</strong></div>
        <span aria-hidden="true">→</span>
        <div><small>체결 후 평단가</small><strong>${formatOptionalPrice(averageAfter, row.ccy)}</strong></div>
        <div><small>매수 수량</small><strong>${Math.round(finite(row.qty)).toLocaleString("ko-KR")}주</strong></div>
      </div>
      <div class="trade-result">
        <div>
          <small>체결 금액</small>
          <strong>${amountNative === null ? "확인 불가" : formatPrice(amountNative, row.ccy)}</strong>
          <span>${amountKrw === null ? "" : `${formatPrice(amountKrw, "KRW")} 환산`}</span>
        </div>
        <p>${escapeHTML(row.reason || reason)}
          ${positionAfter === null ? "" : ` · 체결 후 ${Math.round(positionAfter).toLocaleString("ko-KR")}주`}
          ${estimatedPrice ? " · 구버전 장부/KIS 평단 기준" : row.verified ? " · 브로커 체결 확인" : ""}
        </p>
      </div>
    </article>`;
  }
  const pnl = optionalNumber(row.realized_pnl_krw);
  const tone = pnl === null ? "" : pnl >= 0 ? "gain" : "loss";
  const reason = tradeReasonLabel(row.reason_kind);
  const nativeGap = optionalNumber(row.price_pnl);
  return `<article class="trade-history-card">
    <div class="trade-history-head">
      <div>
        <small>${escapeHTML(tradeDateLabel(row.executed_at))} · ${estimatedPrice ? "잔고 체결·가격 추정" : "체결 확인"}</small>
        <strong>${escapeHTML(row.name || row.code)} <span>${escapeHTML(row.code)}</span></strong>
      </div>
      <div class="badge-row">
        <span class="badge ${row.sleeve === "B" ? "shelf" : "strategy-a"}">전략 ${row.sleeve === "B" ? "B" : "A"}</span>
        <span class="badge trade-reason ${escapeHTML(row.reason_kind)}">${escapeHTML(reason)}</span>
      </div>
    </div>
    <div class="trade-price-flow">
      <div><small>매도 직전 평단가</small><strong>${formatOptionalPrice(row.entry_price, row.ccy)}</strong></div>
      <span aria-hidden="true">→</span>
      <div><small>${estimatedPrice ? "주문가 기준 매도가" : "실제 매도가"}</small><strong>${formatOptionalPrice(row.exit_price, row.ccy)}</strong></div>
      <div><small>매도 수량</small><strong>${Math.round(finite(row.qty)).toLocaleString("ko-KR")}주</strong></div>
    </div>
    <div class="trade-result">
      <div>
        <small>실현손익</small>
        <strong class="${tone}">${pnl === null ? "확인 불가" : formatPrice(pnl, "KRW", true)}</strong>
        <span class="${tone}">${formatOptionalPercent(row.return_pct, 2)}</span>
      </div>
      <p>${nativeGap === null ? "" : `가격차 기준 ${formatPrice(nativeGap, row.ccy, true)} · `}
        ${escapeHTML(row.reason || reason)}
        ${row.partial_exit ? ` · 부분매도 후 ${Math.round(finite(row.remaining_qty)).toLocaleString("ko-KR")}주 보유` : " · 전량/잔량 청산"}
      </p>
    </div>
  </article>`;
}

function tradeHistoryMarkup() {
  if (state.tradesError || !state.trades) {
    return errorState(
      "거래이력을 불러오지 못했어요",
      "보유자산은 그대로 유지됩니다. 로컬 체결 원장 연결을 확인해 주세요.");
  }
  if (state.trades.available === false) {
    return `<div class="alert error">${escapeHTML(state.trades.message || "원장 무결성 확인이 필요합니다.")}</div>`;
  }
  const rows = filteredTrades();
  const controls = `<div class="trade-filter-bar">
    <div class="chart-range-row" aria-label="전략 필터">
      ${[["all", "전체 전략"], ["A", "전략 A"], ["B", "전략 B"]]
        .map(([value, label]) => `<button type="button" class="chart-chip ${state.tradeSleeve === value ? "active" : ""}" data-trade-sleeve="${value}">${label}</button>`).join("")}
    </div>
    <div class="chart-range-row" aria-label="매수 매도 필터">
      ${[["all", "매수·매도"], ["buy", "매수"], ["sell", "매도"]]
        .map(([value, label]) => `<button type="button" class="chart-chip ${state.tradeSide === value ? "active" : ""}" data-trade-side="${value}">${label}</button>`).join("")}
    </div>
    <div class="chart-range-row" aria-label="청산 결과 필터">
      ${[["all", "전체"], ["stop", "손절"], ["take_profit", "익절"], ["win", "수익"], ["loss", "손실"]]
        .map(([value, label]) => `<button type="button" class="chart-chip ${state.tradeOutcome === value ? "active" : ""}" data-trade-outcome="${value}">${label}</button>`).join("")}
    </div>
  </div>`;
  const warning = state.trades.partial
    ? `<div class="alert">일부 기존 거래는 확정 체결 원장 도입 전 기록이라 정확값이 없는 항목을 제외하거나 표시하지 않았습니다.</div>`
    : "";
  return `${controls}${warning}${tradeHistorySummary(rows)}
    <p class="history-note">${escapeHTML(state.trades.message || "")}</p>
    ${rows.length
      ? `<div class="trade-history-list">${rows.map(tradeHistoryCard).join("")}</div>`
      : emptyState("조건에 맞는 거래가 없어요", "필터를 바꾸거나 새 확정 체결이 기록될 때까지 기다려 주세요.")}`;
}

function postExitMetric(event, horizon = state.postExitHorizon) {
  return event?.observations?.[String(horizon)] || null;
}

function filteredPostExitEvents() {
  const rows = [...(state.postExit?.events || [])];
  return state.postExitQuality === "all"
    ? rows : rows.filter((row) => row.quality === state.postExitQuality);
}

function postExitSummaryMarkup(events) {
  const horizon = String(state.postExitHorizon);
  const completedVerified = events.filter((event) =>
    event.quality === "verified" && postExitMetric(event, horizon)?.complete);
  const observations = completedVerified
    .map((event) => postExitMetric(event, horizon))
    .filter(Boolean);
  const average = (key) => observations.length
    ? observations.reduce((sum, row) => sum + finite(row[key]), 0) / observations.length
    : null;
  const continued = observations.length
    ? observations.filter((row) => finite(row.close_vs_exit_pct) > 0).length /
      observations.length * 100 : null;
  return `<div class="trade-summary post-exit-summary">
    <div class="summary-card"><small>수익 매도</small><strong>${events.length}</strong><span>선택 필터</span></div>
    <div class="summary-card"><small>${horizon}일 완료 · 확정가</small><strong>${observations.length}</strong><span>추정가는 통계 제외</span></div>
    <div class="summary-card"><small>평단 기준 추가 수익</small><strong>${average("additional_entry_points_after_exit") === null ? "—" : formatPercent(average("additional_entry_points_after_exit"), 2).replace("%", "%p")}</strong><span>익절 뒤 최고가 평균</span></div>
    <div class="summary-card"><small>매도가보다 높은 종가</small><strong>${continued === null ? "—" : `${continued.toFixed(0)}%`}</strong><span>${horizon}거래일 뒤</span></div>
  </div>`;
}

function postExitTraitMarkup() {
  const horizon = String(state.postExitHorizon);
  if (state.postExitQuality === "estimated") return "";
  if (!["5", "20"].includes(horizon)) return "";
  const traits = (state.postExit?.traits?.[horizon] || [])
    .filter((row) => row.conclusion_ready);
  if (!traits.length) {
    return `<section class="post-exit-traits pending">
      <div><small>공통점 분석</small><strong>확정 체결 표본을 더 모으는 중</strong></div>
      <p>같은 전략·청산사유·부분/전량 표본이 최소 3건 쌓여야 우연을 공통점으로 오해하지 않습니다.</p>
    </section>`;
  }
  return `<section class="post-exit-traits">
    <div class="post-exit-section-head">
      <div><small>공통점 분석 · ${horizon}거래일</small><strong>추가 상승이 컸던 묶음</strong></div>
      <span class="badge strategy-a">확정 체결만</span>
    </div>
    <div class="post-exit-trait-grid">${traits.slice(0, 6).map((row) => `
      <article>
        <small>${escapeHTML(row.label)} · ${row.sample}건</small>
        <strong>${formatOptionalPercent(row.avg_missed_upside_vs_exit_pct, 2)}</strong>
        <p>매도가 뒤 최고가 평균 · 종가 상승 ${finite(row.continued_higher_close_rate).toFixed(0)}%</p>
      </article>`).join("")}</div>
  </section>`;
}

function postExitCard(event) {
  const horizon = String(state.postExitHorizon);
  const row = postExitMetric(event, horizon);
  const quality = event.quality === "verified" ? "브로커 체결가" : "구버전 주문가 추정";
  const status = !row ? "다음 거래일 대기"
    : row.complete ? `${horizon}거래일 관측 완료`
      : `${row.observed_sessions}/${horizon}거래일 관측 중`;
  const peak = row ? formatOptionalPrice(row.peak_price, event.ccy) : "—";
  const total = row ? formatOptionalPercent(row.peak_vs_entry_pct, 2) : "—";
  const added = row
    ? formatOptionalPercent(row.additional_entry_points_after_exit, 2).replace("%", "%p")
    : "—";
  const missed = row ? formatOptionalPercent(row.missed_upside_vs_exit_pct, 2) : "—";
  return `<article class="trade-history-card post-exit-card ${event.quality}">
    <div class="trade-history-head">
      <div>
        <small>${escapeHTML(tradeDateLabel(event.executed_at))} · ${escapeHTML(status)}</small>
        <strong>${escapeHTML(event.name || event.code)} <span>${escapeHTML(event.code)}</span></strong>
      </div>
      <div class="badge-row">
        <span class="badge ${event.sleeve === "B" ? "shelf" : "strategy-a"}">전략 ${event.sleeve}</span>
        <span class="badge ${event.quality === "verified" ? "verified" : "estimated"}">${escapeHTML(quality)}</span>
      </div>
    </div>
    <div class="trade-price-flow post-exit-flow">
      <div><small>익절 당시 평단가</small><strong>${formatOptionalPrice(event.entry_price, event.ccy)}</strong></div>
      <span aria-hidden="true">→</span>
      <div><small>익절가</small><strong>${formatOptionalPrice(event.exit_price, event.ccy)}</strong></div>
      <span aria-hidden="true">→</span>
      <div><small>이후 ${horizon}일 최고가</small><strong>${peak}</strong></div>
    </div>
    <div class="post-exit-metrics">
      <div><small>평단 대비 총 상승</small><strong>${total}</strong></div>
      <div><small>익절 뒤 평단 기준 추가</small><strong class="${row && finite(row.additional_entry_points_after_exit) > 0 ? "gain" : "loss"}">${added}</strong></div>
      <div><small>매도가 대비 놓친 상승</small><strong class="${row && finite(row.missed_upside_vs_exit_pct) > 0 ? "gain" : "loss"}">${missed}</strong></div>
      <div><small>${horizon}일 뒤 종가</small><strong>${row ? formatOptionalPercent(row.close_vs_exit_pct, 2) : "—"}</strong></div>
    </div>
    <p class="post-exit-reason">${escapeHTML(event.reason || "수익 매도")}
      ${event.partial_exit ? " · 부분익절" : " · 전량익절"}
      ${row?.through_date ? ` · ${escapeHTML(row.through_date)}까지` : ""}</p>
  </article>`;
}

function postExitMarkup() {
  if (state.postExitError || !state.postExit) {
    return errorState(
      "익절 사후추적을 불러오지 못했어요",
      "주문·보유 관리는 그대로입니다. 별도 일봉 분석기 상태만 확인해 주세요.");
  }
  if (state.postExit.available === false) {
    return `<div class="alert">${escapeHTML(state.postExit.message || "첫 일봉 갱신을 기다리는 중입니다.")}</div>`;
  }
  const events = filteredPostExitEvents();
  const controls = `<div class="trade-filter-bar post-exit-controls">
    <div class="chart-range-row" aria-label="사후추적 거래일">
      ${(state.postExit.horizons || [1, 3, 5, 10, 20]).map((value) =>
        `<button type="button" class="chart-chip ${String(value) === String(state.postExitHorizon) ? "active" : ""}" data-post-exit-horizon="${value}">${value}일</button>`).join("")}
    </div>
    <div class="chart-range-row" aria-label="매도가 품질">
      ${[["all", "전체"], ["verified", "확정 체결가"], ["estimated", "추정가 참고"]]
        .map(([value, label]) => `<button type="button" class="chart-chip ${state.postExitQuality === value ? "active" : ""}" data-post-exit-quality="${value}">${label}</button>`).join("")}
    </div>
  </div>`;
  return `${controls}${postExitSummaryMarkup(events)}
    <div class="post-exit-explainer">
      <strong>세 숫자를 함께 보세요</strong>
      <p><b>평단 대비 총 상승</b>은 원래 매수가에서 이후 최고가까지,
      <b>익절 뒤 추가(%p)</b>는 익절 뒤 평단 기준으로 더 붙은 수익,
      <b>매도가 대비 놓친 상승</b>은 팔지 않았다면 더 얻을 수 있었던 비율입니다.</p>
    </div>
    ${postExitTraitMarkup()}
    <p class="history-note">${escapeHTML(state.postExit.message || "")}</p>
    ${events.length
      ? `<div class="trade-history-list">${events.map(postExitCard).join("")}</div>`
      : emptyState("추적할 수익 매도가 없어요", "새 수익 매도가 확정되면 다음 거래일부터 자동으로 기록합니다.")}`;
}

function bindPortfolioControls(positions) {
  $$("[data-portfolio-mode]", content).forEach((button) =>
    button.addEventListener("click", () => {
      state.portfolioMode = button.dataset.portfolioMode;
      updateHero();
      renderPortfolio();
    }));
  $$("[data-trade-sleeve]", content).forEach((button) =>
    button.addEventListener("click", () => {
      state.tradeSleeve = button.dataset.tradeSleeve;
      renderPortfolio();
    }));
  $$("[data-trade-outcome]", content).forEach((button) =>
    button.addEventListener("click", () => {
      state.tradeOutcome = button.dataset.tradeOutcome;
      renderPortfolio();
    }));
  $$("[data-trade-side]", content).forEach((button) =>
    button.addEventListener("click", () => {
      state.tradeSide = button.dataset.tradeSide;
      if (state.tradeSide === "buy") state.tradeOutcome = "all";
      renderPortfolio();
    }));
  $$("[data-post-exit-horizon]", content).forEach((button) =>
    button.addEventListener("click", () => {
      state.postExitHorizon = button.dataset.postExitHorizon;
      renderPortfolio();
    }));
  $$("[data-post-exit-quality]", content).forEach((button) =>
    button.addEventListener("click", () => {
      state.postExitQuality = button.dataset.postExitQuality;
      renderPortfolio();
    }));
  $$(".portfolio-card", content).forEach((card) => {
    card.addEventListener("click", () => {
      const position = positions.find((item) => item.code === card.dataset.position);
      if (position) openPortfolioDetail(position);
    });
  });
}

function renderPortfolio() {
  if (!state.portfolio) {
    content.innerHTML = `<div class="local-gate">
      <span class="state-icon">⌂</span>
      <h2>내 자산은 내 컴퓨터에서만 보여요</h2>
      <p>실제 보유 종목과 손익은 공개 사이트로 전송하지 않습니다. 로컬 읽기 전용 대시보드를 실행하면 KIS 조회 정보가 이 화면에만 나타납니다.</p>
    </div>`;
    return;
  }
  const positions = state.portfolio.positions || [];
  const totals = portfolioTotals(positions);
  const summary = Object.entries(totals).map(([ccy, row]) => {
    const rate = row.buy > 0 ? row.pl / row.buy * 100 : 0;
    const tone = row.pl >= 0 ? "gain" : "loss";
    return `<div class="summary-card">
      <small>${ccy === "KRW" ? "한국 주식" : "미국 주식"} 평가금액</small>
      <strong>${formatPrice(row.eval, ccy)}</strong>
      <div class="${tone}">${formatPrice(row.pl, ccy, true)} · ${formatPercent(rate)}</div>
    </div>`;
  }).join("");
  const partial = state.portfolio.partial
    ? `<div class="alert">일부 시장 조회가 지연돼 목록이 완전하지 않을 수 있습니다.</div>` : "";
  const age = state.portfolio.price_age_seconds;
  const liveText = Number.isFinite(Number(age))
    ? `현재가 약 ${Math.max(0, Math.round(Number(age)))}초 전`
    : `${finite(state.portfolio.refresh_seconds, 60)}초마다 잔고 갱신`;
  content.innerHTML = `
    <div class="private-banner"><span>●</span> ${escapeHTML(state.portfolio.environment || "KIS")} 계좌
      · ${escapeHTML(liveText)}
      · 주문 기능과 분리된 로컬 조회</div>
    ${partial}
    ${portfolioTabsMarkup(positions)}
    ${state.portfolioMode === "history"
      ? tradeHistoryMarkup()
      : state.portfolioMode === "post-exit"
        ? postExitMarkup()
      : positions.length
        ? `<div class="portfolio-summary">${summary}</div>
           <div class="portfolio-grid">${positions.map(portfolioCard).join("")}</div>`
        : emptyState("현재 보유 종목이 없어요", "연결된 KIS 환경의 보유 잔고가 0건입니다.")}`;
  bindPortfolioControls(positions);
}

function openPortfolioDetail(position) {
  $("#detail-market").textContent = `${marketLabel(position.ccy)} · ${position.code}`;
  $("#detail-title").textContent = position.name || position.code;
  dialog.dataset.position = position.code;
  dialog.dataset.chartMode = isPrivateDashboard() ? "live" : "daily";
  dialog.dataset.chartDays = "90";
  $("#detail-content").innerHTML = `
    <div id="detail-position-summary">${portfolioDetailSummaryMarkup(position)}</div>
    <div class="chart-wrap">
      <div class="chart-head">
        <div>
          <strong id="chart-title">준실시간 가격</strong>
          <span id="chart-updated">파수꾼 시세 공유 · KIS 추가 호출 없음</span>
        </div>
        <div class="chart-mode-row">
          <button class="chart-chip active" type="button" data-chart-mode="live">실시간</button>
          <button class="chart-chip" type="button" data-chart-mode="daily">일봉</button>
        </div>
      </div>
      <div class="chart-range-row daily-ranges hidden">
        <button class="chart-chip" type="button" data-chart-days="30">1개월</button>
        <button class="chart-chip active" type="button" data-chart-days="90">3개월</button>
        <button class="chart-chip" type="button" data-chart-days="180">6개월</button>
        <button class="chart-chip" type="button" data-chart-days="365">1년</button>
      </div>
      <div class="chart-loading" id="chart-loading">가격 그래프를 불러오고 있어요.</div>
      <canvas id="price-chart" class="hidden" role="img" aria-label="${escapeHTML(position.name)} 가격 차트"></canvas>
      <div class="chart-legend" id="price-chart-legend"></div>
    </div>`;
  dialog.showModal();
  $$("[data-chart-mode]", $("#detail-content")).forEach((button) => {
    button.addEventListener("click", () => {
      dialog.dataset.chartMode = button.dataset.chartMode;
      $$("[data-chart-mode]", $("#detail-content")).forEach((item) =>
        item.classList.toggle("active", item === button));
      $(".daily-ranges", $("#detail-content")).classList.toggle(
        "hidden", button.dataset.chartMode !== "daily");
      loadPortfolioChart(position);
    });
  });
  $$("[data-chart-days]", $("#detail-content")).forEach((button) => {
    button.addEventListener("click", () => {
      dialog.dataset.chartDays = button.dataset.chartDays;
      $$("[data-chart-days]", $("#detail-content")).forEach((item) =>
        item.classList.toggle("active", item === button));
      loadPortfolioChart(position);
    });
  });
  loadPortfolioChart(position);
}

function portfolioDetailSummaryMarkup(position) {
  const metrics = positionInvestmentSummary(position);
  const opened = formatOpenedDate(position.opened);
  const tone = metrics.pnlAmount === null
    ? "" : metrics.pnlAmount >= 0 ? "gain" : "loss";
  const sleeve = String(position.sleeve || "A").toUpperCase() === "B" ? "B" : "A";
  const quantity = metrics.quantity === null
    ? "—" : `${metrics.quantity.toLocaleString("ko-KR")}주`;
  let priceInsight = "평균매수가와 현재가를 함께 확인할 수 없습니다.";
  if (metrics.priceGapPct !== null && metrics.priceGapPct >= 0) {
    priceInsight = `현재가는 평균매수가보다 ${formatPercent(metrics.priceGapPct)} 위입니다.`;
  } else if (metrics.breakEvenMovePct > 0) {
    priceInsight = `현재가에서 평균매수가 회복까지 +${metrics.breakEvenMovePct.toFixed(1)}%가 필요합니다.`;
  }
  const openedMarkup = opened
    ? `<strong>${escapeHTML(opened.label)} 매수 · 보유 ${opened.holdingDays}일째</strong>
       <p>봇의 확정 체결 원장에 기록된 최초 매수일 기준입니다.</p>`
    : `<strong>매수일 미확인</strong>
       <p>KIS 잔고에 최초 매수일이 없는 수동·기존 보유 종목은 날짜를 추정하지 않습니다.</p>`;
  return `
    <div class="detail-price-row">
      <div class="detail-current">
        <small>현재가</small>
        <div class="detail-price">${formatOptionalPrice(metrics.currentPrice, position.ccy)}</div>
        <span class="${tone}">${formatOptionalPercent(metrics.returnPct)}
          · ${formatOptionalPrice(metrics.pnlAmount, position.ccy, true)}</span>
      </div>
      <span class="badge ${sleeve === "B" ? "shelf" : "strategy-a"}">전략 ${sleeve}</span>
    </div>
    <section class="purchase-section" aria-label="내 매수 정보">
      <div class="detail-section-head">
        <strong>내 매수 정보</strong>
        <span>KIS 잔고 기준</span>
      </div>
      <div class="detail-grid">
        <div class="detail-box emphasis"><small>내 평균매수가</small><strong>${formatOptionalPrice(metrics.averagePrice, position.ccy)}</strong></div>
        <div class="detail-box"><small>보유 수량</small><strong>${quantity}</strong></div>
        <div class="detail-box"><small>1주당 손익</small><strong class="${tone}">${formatOptionalPrice(metrics.perSharePnl, position.ccy, true)}</strong></div>
        <div class="detail-box"><small>총 투입금</small><strong>${formatOptionalPrice(metrics.investedAmount, position.ccy)}</strong></div>
        <div class="detail-box"><small>현재 평가금</small><strong>${formatOptionalPrice(metrics.currentValue, position.ccy)}</strong></div>
        <div class="detail-box"><small>총 평가손익</small><strong class="${tone}">${formatOptionalPrice(metrics.pnlAmount, position.ccy, true)}</strong></div>
      </div>
      <div class="purchase-insight">
        <div class="purchase-date ${opened ? "" : "unknown"}">${openedMarkup}</div>
        <div class="price-insight"><small>가격 분석</small><strong>${escapeHTML(priceInsight)}</strong></div>
      </div>
    </section>
    ${positionPlanDetailMarkup(position)}`;
}

function refreshOpenPortfolioDetail() {
  if (!dialog.open || !dialog.dataset.position) return;
  const position = (state.portfolio?.positions || []).find(
    (item) => item.code === dialog.dataset.position);
  if (!position) return;
  $("#detail-market").textContent = `${marketLabel(position.ccy)} · ${position.code}`;
  $("#detail-title").textContent = position.name || position.code;
  const summary = $("#detail-position-summary");
  if (summary) summary.innerHTML = portfolioDetailSummaryMarkup(position);
  if (dialog.dataset.chartMode === "live") loadPortfolioChart(position, { quiet: true });
  else {
    const canvas = $("#price-chart");
    if (canvas?._chartData) {
      canvas._chartData.position = position;
      redrawChart(canvas);
    }
  }
}

async function loadPortfolioChart(position, { quiet = false } = {}) {
  const loading = $("#chart-loading");
  const canvas = $("#price-chart");
  const legend = $("#price-chart-legend");
  if (!loading || !canvas) return;
  if (!quiet) {
    loading.classList.remove("hidden");
    canvas.classList.add("hidden");
    legend.innerHTML = "";
  }
  const mode = dialog.dataset.chartMode || "live";
  try {
    if (mode === "live") {
      const url = `${API.quotes}?code=${encodeURIComponent(position.code)}&limit=900`;
      const chart = await fetchJSON(url, 10000);
      const points = [...(chart.points || [])];
      if (!points.length && finite(position.cur) > 0) {
        points.push({ ts: Date.now() / 1000, price: finite(position.cur) });
      }
      if (!points.length) throw new Error("empty live chart");
      $("#chart-title").textContent = "준실시간 가격";
      const age = state.portfolio?.price_age_seconds;
      $("#chart-updated").textContent = Number.isFinite(Number(age))
        ? `현재가 약 ${Math.round(Number(age))}초 전 · 파수꾼 공유`
        : "파수꾼 시세가 쌓이는 중";
      loading.classList.add("hidden");
      canvas.classList.remove("hidden");
      drawLivePriceChart(canvas, points, position);
      legend.innerHTML = chartLegend([
        ["current", "현재가", "blue"], ["average", "평균매수가", "violet"],
        ["stop", "손절", "red"], ["target", "목표", "green"],
      ], priceChartHidden.live);
      bindPriceChartLegend(canvas, "live");
    } else {
      const days = Number(dialog.dataset.chartDays || 90);
      const url = `${API.chart}?code=${encodeURIComponent(position.code)}&days=${days}`;
      const chart = await fetchJSON(url, 30000);
      if (!chart.points?.length) throw new Error("empty daily chart");
      $("#chart-title").textContent = `${days >= 365 ? "1년" : days >= 180 ? "6개월" : days >= 90 ? "3개월" : "1개월"} 일봉`;
      $("#chart-updated").textContent = "캔들·거래량·이동평균 · 현재가는 준실시간";
      loading.classList.add("hidden");
      canvas.classList.remove("hidden");
      drawCandleChart(canvas, chart.points, position);
      legend.innerHTML = chartLegend([
        ["ma20", "MA20", "blue"], ["ma60", "MA60", "amber"],
        ["ma120", "MA120", "violet"], ["average", "평균매수가", "violet"],
        ["stop", "손절", "red"], ["target", "목표", "green"],
        ["current", "현재가", "blue"],
      ], priceChartHidden.daily);
      bindPriceChartLegend(canvas, "daily");
    }
    loading.classList.add("hidden");
    canvas.classList.remove("hidden");
  } catch (error) {
    if (!quiet) {
      loading.textContent = mode === "live"
        ? "준실시간 시세가 아직 쌓이지 않았습니다. 일봉 차트는 바로 볼 수 있어요."
        : "가격 이력을 불러오지 못했습니다. 보유·현재가 정보는 정상적으로 유지됩니다.";
    }
  }
}

function chartLegend(items, hidden = new Set()) {
  return items.map(([key, label, tone]) => {
    const active = !hidden.has(key);
    return `<button type="button" class="${active ? "active" : ""}"
      data-chart-series="${escapeHTML(key)}" aria-pressed="${active}">
      <i class="legend-${tone}"></i>${escapeHTML(label)}
    </button>`;
  }).join("");
}

function bindPriceChartLegend(canvas, mode) {
  $$("[data-chart-series]", $("#price-chart-legend")).forEach((button) =>
    button.addEventListener("click", () => {
      const key = button.dataset.chartSeries;
      const hidden = priceChartHidden[mode];
      if (hidden.has(key)) hidden.delete(key);
      else hidden.add(key);
      button.classList.toggle("active", !hidden.has(key));
      button.setAttribute("aria-pressed", String(!hidden.has(key)));
      redrawChart(canvas);
    }));
}

function chartCanvas(canvas, height = 320) {
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(300, rect.width);
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  const css = getComputedStyle(document.documentElement);
  ctx.clearRect(0, 0, width, height);
  return {
    ctx, css, width, height,
    colors: {
      blue: css.getPropertyValue("--blue").trim(),
      green: css.getPropertyValue("--green").trim(),
      red: css.getPropertyValue("--red").trim(),
      amber: css.getPropertyValue("--amber").trim(),
      violet: css.getPropertyValue("--violet").trim(),
      grid: css.getPropertyValue("--line").trim(),
      muted: css.getPropertyValue("--muted").trim(),
      surface: css.getPropertyValue("--surface").trim(),
    },
  };
}

function chartGrid(ctx, colors, width, top, bottom, left, right) {
  ctx.strokeStyle = colors.grid;
  ctx.lineWidth = 1;
  for (let i = 0; i < 4; i += 1) {
    const gy = top + i / 3 * (bottom - top);
    ctx.beginPath();
    ctx.moveTo(left, gy);
    ctx.lineTo(width - right, gy);
    ctx.stroke();
  }
}

function chartReferenceLines(ctx, colors, position, y, left, rightX, hidden) {
  const lines = [
    ["average", "평균", finite(position.avg), colors.violet],
    ["stop", "손절", finite(position.stop), colors.red],
    ["target", "목표", finite(position.target), colors.green],
  ].filter(([key, , value]) => value > 0 && !hidden.has(key));
  ctx.font = "10px -apple-system, sans-serif";
  lines.forEach(([, label, value, lineColor]) => {
    const lineY = y(value);
    ctx.save();
    ctx.setLineDash([5, 4]);
    ctx.strokeStyle = lineColor;
    ctx.beginPath();
    ctx.moveTo(left, lineY);
    ctx.lineTo(rightX, lineY);
    ctx.stroke();
    ctx.restore();
    ctx.fillStyle = lineColor;
    ctx.fillText(label, rightX + 5, lineY + 3);
  });
}

function drawLivePriceChart(canvas, points, position) {
  canvas._chartData = { kind: "live", points, position };
  const hidden = priceChartHidden.live;
  const { ctx, colors, width, height } = chartCanvas(canvas);
  const pad = { left: 12, right: 50, top: 18, bottom: 28 };
  const values = points.map((point) => finite(point.price)).filter((value) => value > 0);
  const refs = [position.avg, position.stop, position.target].map(finite).filter((value) => value > 0);
  const min = Math.min(...values, ...refs);
  const max = Math.max(...values, ...refs);
  const span = Math.max(max - min, max * .01, 1e-6);
  const x = (index) => pad.left + index / Math.max(1, values.length - 1) *
    (width - pad.left - pad.right);
  const y = (value) => pad.top + (max - value) / span *
    (height - pad.top - pad.bottom);
  chartGrid(ctx, colors, width, pad.top, height - pad.bottom, pad.left, pad.right);
  if (!hidden.has("current")) {
    const gradient = ctx.createLinearGradient(0, pad.top, 0, height - pad.bottom);
    gradient.addColorStop(0, `${colors.blue}48`);
    gradient.addColorStop(1, `${colors.blue}00`);
    ctx.beginPath();
    values.forEach((value, index) => index
      ? ctx.lineTo(x(index), y(value))
      : ctx.moveTo(x(index), y(value)));
    ctx.lineTo(x(values.length - 1), height - pad.bottom);
    ctx.lineTo(x(0), height - pad.bottom);
    ctx.closePath();
    ctx.fillStyle = gradient;
    ctx.fill();
    ctx.beginPath();
    values.forEach((value, index) => index
      ? ctx.lineTo(x(index), y(value))
      : ctx.moveTo(x(index), y(value)));
    ctx.strokeStyle = colors.blue;
    ctx.lineWidth = 2.5;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.stroke();
    const lastX = x(values.length - 1);
    const lastY = y(values.at(-1));
    ctx.beginPath();
    ctx.arc(lastX, lastY, 4, 0, Math.PI * 2);
    ctx.fillStyle = colors.blue;
    ctx.fill();
  }
  chartReferenceLines(
    ctx, colors, position, y, pad.left, width - pad.right, hidden);
  ctx.fillStyle = colors.muted;
  ctx.font = "10px -apple-system, sans-serif";
  const timeLabel = (point) => new Date(finite(point.ts) * 1000).toLocaleTimeString(
    "ko-KR", { hour: "2-digit", minute: "2-digit" });
  ctx.fillText(timeLabel(points[0]), pad.left, height - 7);
  const lastLabel = timeLabel(points.at(-1));
  ctx.fillText(lastLabel, width - pad.right - ctx.measureText(lastLabel).width, height - 7);
}

function drawSeries(ctx, points, key, color, x, y) {
  let started = false;
  ctx.beginPath();
  points.forEach((point, index) => {
    const value = Number(point[key]);
    if (!Number.isFinite(value) || value <= 0) return;
    if (!started) {
      ctx.moveTo(x(index), y(value));
      started = true;
    } else {
      ctx.lineTo(x(index), y(value));
    }
  });
  if (started) {
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.35;
    ctx.lineJoin = "round";
    ctx.stroke();
  }
}

function drawCandleChart(canvas, points, position) {
  canvas._chartData = { kind: "daily", points, position };
  const hidden = priceChartHidden.daily;
  const { ctx, colors, width, height } = chartCanvas(canvas);
  const pad = { left: 12, right: 50, top: 16, bottom: 25 };
  const volumeHeight = 58;
  const priceBottom = height - pad.bottom - volumeHeight - 12;
  const priceValues = points.flatMap((point) =>
    [finite(point.high), finite(point.low), finite(point.ma20),
      finite(point.ma60), finite(point.ma120)]).filter((value) => value > 0);
  const refs = [position.avg, position.stop, position.target, position.cur]
    .map(finite).filter((value) => value > 0);
  const min = Math.min(...priceValues, ...refs);
  const max = Math.max(...priceValues, ...refs);
  const span = Math.max(max - min, max * .02, 1e-6);
  const step = (width - pad.left - pad.right) / Math.max(1, points.length);
  const x = (index) => pad.left + step * (index + .5);
  const y = (value) => pad.top + (max - value) / span * (priceBottom - pad.top);
  const maxVolume = Math.max(...points.map((point) => finite(point.volume)), 1);
  chartGrid(ctx, colors, width, pad.top, priceBottom, pad.left, pad.right);
  points.forEach((point, index) => {
    const open = finite(point.open), close = finite(point.close);
    const high = finite(point.high), low = finite(point.low);
    if (!(open > 0 && close > 0 && high > 0 && low > 0)) return;
    const up = close >= open;
    const color = up ? colors.green : colors.red;
    const center = x(index);
    ctx.strokeStyle = color;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(center, y(high));
    ctx.lineTo(center, y(low));
    ctx.stroke();
    const bodyTop = Math.min(y(open), y(close));
    const bodyHeight = Math.max(1.5, Math.abs(y(open) - y(close)));
    const bodyWidth = Math.max(1.5, Math.min(8, step * .64));
    ctx.fillStyle = color;
    ctx.fillRect(center - bodyWidth / 2, bodyTop, bodyWidth, bodyHeight);
    const volume = finite(point.volume);
    const volumeTop = height - pad.bottom - volume / maxVolume * volumeHeight;
    ctx.globalAlpha = .35;
    ctx.fillRect(center - bodyWidth / 2, volumeTop, bodyWidth,
      height - pad.bottom - volumeTop);
    ctx.globalAlpha = 1;
  });
  if (!hidden.has("ma20")) drawSeries(ctx, points, "ma20", colors.blue, x, y);
  if (!hidden.has("ma60")) drawSeries(ctx, points, "ma60", colors.amber, x, y);
  if (!hidden.has("ma120")) drawSeries(ctx, points, "ma120", colors.violet, x, y);
  chartReferenceLines(
    ctx, colors, position, y, pad.left, width - pad.right, hidden);
  if (!hidden.has("current") && finite(position.cur) > 0) {
    const liveY = y(finite(position.cur));
    ctx.fillStyle = colors.blue;
    ctx.fillRect(width - pad.right - 4, liveY - 2, 8, 4);
  }
  ctx.fillStyle = colors.muted;
  ctx.font = "10px -apple-system, sans-serif";
  ctx.fillText(points[0].date.slice(2), pad.left, height - 7);
  const lastLabel = points.at(-1).date.slice(2);
  ctx.fillText(lastLabel, width - pad.right - ctx.measureText(lastLabel).width, height - 7);
}

function redrawChart(canvas) {
  const data = canvas?._chartData;
  if (!data) return;
  if (data.kind === "live") drawLivePriceChart(canvas, data.points, data.position);
  else if (data.kind === "daily") drawCandleChart(canvas, data.points, data.position);
  else if (data.kind === "performance") {
    drawPerformanceChart(canvas, data.rows, data.keys, data.labels);
  }
}

function readTrackStat(...keys) {
  const candidates = [state.track?.stats, state.track, state.track?.stats?.all].filter(Boolean);
  for (const source of candidates) {
    for (const key of keys) {
      if (source[key] !== undefined && source[key] !== null) return source[key];
    }
  }
  return null;
}

function performanceRows(market, range) {
  const marketDoc = state.performance?.markets?.[market] || {};
  const indexNames = marketDoc.indices || [];
  if (range === "today") {
    return (marketDoc.series || []).map((point) => {
      const coverage = completeHoldingsValue(point.holdings);
      return {
        label: point.t,
        account: point.account,
        A: point.A,
        B: point.B,
        holdings: coverage.value,
        holdingsCovered: coverage.covered,
        holdingsEligible: coverage.eligible,
        ...Object.fromEntries(indexNames.map((name) =>
          [`idx:${name}`, point.indices?.[name]])),
        ...Object.fromEntries(indexNames.map((name) =>
          [`dailyidx:${name}`, point.daily_indices?.[name]])),
      };
    });
  }
  const limit = range === "1m" ? 30 : range === "3m" ? 90 : 10000;
  const daily = (state.performance?.days || [])
    .filter((row) => row.market === market)
    .slice(-limit)
    .map((row) => {
      const coverage = completeHoldingsValue(row.holdings);
      return {
        label: row.date?.slice(5) || "—",
        account: row.account,
        A: row.A,
        B: row.B,
        holdings: coverage.value,
        holdingsCovered: coverage.covered,
        holdingsEligible: coverage.eligible,
        ...Object.fromEntries(indexNames.map((name) =>
          [`idx:${name}`, row.daily_indices?.[name] ?? row.indices?.[name]])),
        ...Object.fromEntries(indexNames.map((name) =>
          [`dailyidx:${name}`, row.daily_indices?.[name] ?? row.indices?.[name]])),
      };
    });
  const current = marketDoc.series?.at(-1);
  if (current && marketDoc.date && !daily.some((row) => row.label === marketDoc.date.slice(5))) {
    const coverage = completeHoldingsValue(current.holdings);
    daily.push({
      label: marketDoc.date.slice(5),
      account: current.account,
      A: current.A,
      B: current.B,
      holdings: coverage.value,
      holdingsCovered: coverage.covered,
      holdingsEligible: coverage.eligible,
      ...Object.fromEntries(indexNames.map((name) =>
        [`idx:${name}`, current.indices?.[name]])),
      ...Object.fromEntries(indexNames.map((name) =>
        [`dailyidx:${name}`, current.daily_indices?.[name]])),
    });
  }
  const holdingsSeries = completeHoldingsSeries(daily.map((row) => ({
    account: row.holdings,
    covered: row.holdingsCovered,
    eligible: row.holdingsEligible,
  })));
  const keys = ["account", "A", "B", "holdings",
    ...indexNames.map((name) => `idx:${name}`)];
  const cumulative = Object.fromEntries(keys.map((key) => [key, 0]));
  return daily.map((row) => {
    const out = { label: row.label };
    keys.forEach((key) => {
      if (key === "holdings" && !holdingsSeries.complete) {
        out[key] = null;
        return;
      }
      if (row[key] === null || row[key] === undefined) {
        out[key] = null;
        return;
      }
      const value = Number(row[key]);
      if (!Number.isFinite(value)) {
        out[key] = null;
        return;
      }
      cumulative[key] = ((1 + cumulative[key] / 100) * (1 + value / 100) - 1) * 100;
      out[key] = cumulative[key];
    });
    out.holdingsCovered = row.holdingsCovered;
    out.holdingsEligible = row.holdingsEligible;
    out.holdingsRangeComplete = holdingsSeries.complete;
    out.holdingsIncompleteDays = holdingsSeries.incompleteDays;
    return out;
  });
}

function performanceValue(value) {
  return value !== null && value !== undefined && Number.isFinite(Number(value))
    ? formatPercent(Number(value), 2) : "—";
}

function performanceInsights(rows, indexNames) {
  const latest = rows.at(-1) || {};
  const a = optionalNumber(latest.A);
  const b = optionalNumber(latest.B);
  const account = optionalNumber(latest.account);
  const strategyTitle = a === null || b === null ? "전략 비교 기록 중"
    : a === b ? "전략 A·B 동률"
      : `전략 ${a > b ? "A" : "B"} 우세`;
  const strategyDetail = a === null || b === null
    ? "두 전략 값이 모두 쌓이면 차이를 보여드립니다."
    : `두 전략 차이 ${Math.abs(a - b).toFixed(2)}%p`;
  const indices = indexNames.map((name) => ({
    name, value: optionalNumber(latest[`idx:${name}`]),
  })).filter((row) => row.value !== null);
  const beats = account === null ? [] : indices.filter((row) => account > row.value);
  const marketTitle = account === null || !indices.length ? "시장 비교 기록 중"
    : beats.length === indices.length
      ? `KIS가 ${indices.length === 1 ? "지수보다" : "두 지수"} 앞섬`
      : beats.length === 0
        ? `KIS가 ${indices.length === 1 ? "지수보다" : "두 지수"} 뒤처짐`
        : "지수별 성과 엇갈림";
  const marketDetail = indices.map((row) =>
    `${row.name} 대비 ${account === null ? "—" : performanceValue(account - row.value).replace("%", "%p")}`)
    .join(" · ") || "지수 값을 기다리고 있어요.";
  const drawdown = maximumDrawdown(rows);
  const drawdownTitle = drawdown === null ? "낙폭 기록 중"
    : drawdown > -0.01 ? "고점 유지" : `고점 대비 ${drawdown.toFixed(2)}%`;
  const drawdownDetail = drawdown === null
    ? "비교할 시점이 두 개 이상 필요합니다."
    : "선택 기간 중 계좌 평가의 최대 낙폭";
  return `<div class="insight-grid">
    <div class="insight-card"><small>전략 대결</small><strong>${escapeHTML(strategyTitle)}</strong><p>${escapeHTML(strategyDetail)}</p></div>
    <div class="insight-card"><small>시장 판정</small><strong>${escapeHTML(marketTitle)}</strong><p>${escapeHTML(marketDetail)}</p></div>
    <div class="insight-card"><small>흔들림</small><strong>${escapeHTML(drawdownTitle)}</strong><p>${escapeHTML(drawdownDetail)}</p></div>
  </div>`;
}

function renderKisPerformance() {
  const market = state.performanceMarket;
  const marketDoc = state.performance?.markets?.[market] || {};
  const range = state.performanceRange;
  const rows = performanceRows(market, range);
  const latest = rows.at(-1) || {};
  const indexNames = marketDoc.indices || [];
  const primary = indexNames[0];
  const basisLabel = marketDoc.basis === "previous_close"
    ? "전일 마감" : "오늘 첫 수집";
  const epochLabel = String(state.performance?.epoch?.label || "");
  const holdingsValue = optionalNumber(latest.holdings);
  const dailyIndexValue = optionalNumber(latest[`dailyidx:${primary}`])
    ?? optionalNumber(latest[`idx:${primary}`]);
  const holdingsAlpha = holdingsValue !== null && dailyIndexValue !== null
    ? holdingsValue - dailyIndexValue : null;
  const coverage = Number(latest.holdingsCovered || 0);
  const eligible = Number(latest.holdingsEligible || 0);
  const coverageComplete = holdingsValue !== null && eligible > 0
    && coverage === eligible
    && (range === "today" || latest.holdingsRangeComplete === true);
  const coverageDetail = coverageComplete
    ? `${escapeHTML(primary || "주 지수")} 대비 ${performanceValue(holdingsAlpha).replace("%", "%p")} · 전체 ${coverage}/${eligible}종목`
    : range === "today"
      ? `자료 부족 ${coverage}/${eligible}종목 · 전체 종목이 모일 때만 비교`
      : `선택 기간 중 부분수집 ${Number(latest.holdingsIncompleteDays || 0)}일 · 지수 비교 제외`;
  const alphaValue = latest.account !== null && latest.account !== undefined &&
    latest[`idx:${primary}`] !== null && latest[`idx:${primary}`] !== undefined &&
    Number.isFinite(Number(latest.account)) && Number.isFinite(Number(latest[`idx:${primary}`]))
    ? Number(latest.account) - Number(latest[`idx:${primary}`])
    : null;
  const age = relativeMinutes(state.performance?.generated_at);
  content.innerHTML = `
    <div class="performance-controls">
      <div class="chart-range-row">
        <button class="chart-chip ${market === "US" ? "active" : ""}" type="button" data-performance-market="US">미국 · 나스닥/S&amp;P500</button>
        <button class="chart-chip ${market === "KR" ? "active" : ""}" type="button" data-performance-market="KR">한국 · 코스피/코스닥</button>
      </div>
      <div class="chart-range-row">
        ${[["today", "오늘"], ["1m", "1개월"], ["3m", "3개월"], ["all", "전체"]]
          .map(([value, label]) => `<button class="chart-chip ${range === value ? "active" : ""}" type="button" data-performance-range="${value}">${label}</button>`)
          .join("")}
      </div>
    </div>
    ${rows.length ? `
      <div class="performance-grid performance-kis-grid">
        <div class="performance-card"><small>봇 운용자산 TWR</small><strong class="${finite(latest.account) >= 0 ? "gain" : "loss"}">${performanceValue(latest.account)}</strong><p>${range === "today" ? basisLabel : "선택 기간"} 대비</p></div>
        <div class="performance-card"><small>전략 A · 전환</small><strong>${performanceValue(latest.A)}</strong><p>A 보유 종목 기준</p></div>
        <div class="performance-card"><small>전략 B · 매물대</small><strong>${performanceValue(latest.B)}</strong><p>B 보유 종목 기준</p></div>
        <div class="performance-card"><small>${escapeHTML(primary || "주 지수")} 대비</small><strong class="${finite(alphaValue) >= 0 ? "gain" : "loss"}">${performanceValue(alphaValue)}</strong><p>초과수익률(%p)</p></div>
        <div class="performance-card"><small>장 시작 보유 · 동일가중</small><strong class="${coverageComplete && finite(holdingsValue) >= 0 ? "gain" : coverageComplete ? "loss" : ""}">${performanceValue(holdingsValue)}</strong><p>${coverageDetail}</p></div>
      </div>
      ${performanceInsights(rows, indexNames)}
      ${strategyDefinitionMarkup({ compact: true })}
      <div class="performance-chart-card">
        <div class="chart-head">
          <div><strong>${escapeHTML(marketDoc.label || market)} 시장 비교</strong><span>${escapeHTML(epochLabel ? `${epochLabel} · ${basisLabel}` : basisLabel)}에 계좌·지수를 함께 0%로 맞춰 비교</span></div>
          <span class="live-pill">${age === null ? "기록 중" : age === 0 ? "방금 갱신" : `${age}분 전`}</span>
        </div>
        <canvas id="performance-chart" role="img" aria-label="KIS 전략과 시장지수 수익률 비교 차트"></canvas>
        <div class="chart-legend performance-legend" aria-label="비교선 표시 선택">
          ${[
            ["account", "봇 운용자산", "blue"],
            ["A", "전략 A", "green"],
            ["B", "전략 B", "violet"],
            ...indexNames.map((name, index) =>
              [`idx:${name}`, name, index ? "amber" : "muted"]),
          ].map(([key, label, tone]) => {
            const active = !state.performanceHidden.has(key);
            return `<button type="button" class="${active ? "active" : ""}"
              data-performance-series="${escapeHTML(key)}" aria-pressed="${active}">
              <i class="legend-${tone}"></i>${escapeHTML(label)}
            </button>`;
          }).join("")}
        </div>
        <p class="chart-note">위 이름을 누르면 해당 선을 끄거나 다시 켤 수 있습니다. ${escapeHTML(epochLabel || "현재 성과 기준")}부터 장기 누적하며, 이전 손상 구간은 비교에 섞지 않습니다. ${escapeHTML(state.performance?.basis || "KIS 봇 운용자산 NAV/TWR 기준")} · ${finite(state.performance?.sample_seconds, 300) / 60}분 간격.</p>
      </div>` :
      emptyState("지수 비교 데이터를 쌓는 중이에요",
        "장중 첫 수집이 완료되면 전략 A·B와 나스닥·S&P500·코스피·코스닥 차트가 자동으로 나타납니다.")}
  `;
  $$("[data-performance-market]", content).forEach((button) =>
    button.addEventListener("click", () => {
      state.performanceMarket = button.dataset.performanceMarket;
      renderPerformance();
      updateHero();
    }));
  $$("[data-performance-range]", content).forEach((button) =>
    button.addEventListener("click", () => {
      state.performanceRange = button.dataset.performanceRange;
      renderPerformance();
    }));
  $$("[data-performance-series]", content).forEach((button) =>
    button.addEventListener("click", () => {
      const key = button.dataset.performanceSeries;
      if (state.performanceHidden.has(key)) state.performanceHidden.delete(key);
      else state.performanceHidden.add(key);
      button.classList.toggle("active", !state.performanceHidden.has(key));
      button.setAttribute(
        "aria-pressed", String(!state.performanceHidden.has(key)));
      redrawChart($("#performance-chart"));
    }));
  const canvas = $("#performance-chart");
  if (canvas && rows.length) {
    const keys = ["account", "A", "B", ...indexNames.map((name) => `idx:${name}`)];
    const labels = {
      account: "봇 운용자산", A: "전략 A", B: "전략 B",
      ...Object.fromEntries(indexNames.map((name) => [`idx:${name}`, name])),
    };
    requestAnimationFrame(() => drawPerformanceChart(canvas, rows, keys, labels));
  }
}

function drawPerformanceChart(canvas, rows, keys, labels) {
  canvas._chartData = { kind: "performance", rows, keys, labels };
  const visibleKeys = keys.filter((key) => !state.performanceHidden.has(key));
  const { ctx, colors, width, height } = chartCanvas(canvas, 340);
  const pad = { left: 18, right: 48, top: 20, bottom: 28 };
  const values = rows.flatMap((row) => visibleKeys.flatMap((key) =>
    row[key] === null || row[key] === undefined ? [] : [Number(row[key])]))
    .filter(Number.isFinite);
  const rawMin = Math.min(0, ...values);
  const rawMax = Math.max(0, ...values);
  const extra = Math.max((rawMax - rawMin) * .12, .2);
  const min = rawMin - extra, max = rawMax + extra;
  const span = Math.max(max - min, .1);
  const x = (index) => pad.left + index / Math.max(1, rows.length - 1) *
    (width - pad.left - pad.right);
  const y = (value) => pad.top + (max - value) / span *
    (height - pad.top - pad.bottom);
  chartGrid(ctx, colors, width, pad.top, height - pad.bottom, pad.left, pad.right);
  ctx.strokeStyle = colors.muted;
  ctx.globalAlpha = .55;
  ctx.beginPath();
  ctx.moveTo(pad.left, y(0));
  ctx.lineTo(width - pad.right, y(0));
  ctx.stroke();
  ctx.globalAlpha = 1;
  const palette = {
    account: colors.blue, A: colors.green, B: colors.violet,
  };
  const indexKeys = keys.filter((key) => key.startsWith("idx:"));
  visibleKeys.forEach((key) => {
    const color = palette[key] || (
      indexKeys.indexOf(key) > 0 ? colors.amber : colors.muted);
    let started = false;
    ctx.beginPath();
    rows.forEach((row, index) => {
      if (row[key] === null || row[key] === undefined) return;
      const value = Number(row[key]);
      if (!Number.isFinite(value)) return;
      if (!started) {
        ctx.moveTo(x(index), y(value));
        started = true;
      } else {
        ctx.lineTo(x(index), y(value));
      }
    });
    if (started) {
      ctx.strokeStyle = color;
      ctx.lineWidth = key === "account" ? 2.7 : 1.55;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.stroke();
    }
  });
  ctx.fillStyle = colors.muted;
  ctx.font = "10px -apple-system, sans-serif";
  ctx.fillText(rows[0].label || "", pad.left, height - 7);
  const lastLabel = rows.at(-1).label || "";
  ctx.fillText(lastLabel, width - pad.right - ctx.measureText(lastLabel).width, height - 7);
  ctx.fillText(`${max.toFixed(1)}%`, width - pad.right + 5, pad.top + 4);
  ctx.fillText(`${min.toFixed(1)}%`, width - pad.right + 5, height - pad.bottom);
}

function renderPerformance() {
  if (isPrivateDashboard()) {
    renderKisPerformance();
    return;
  }
  renderPaperPerformance();
}

function renderPaperPerformance() {
  const paper = state.paper || {};
  const ret = finite(paper.ret_pct);
  const trades = finite(paper.trades);
  const wins = finite(paper.win_trades);
  const winRate = trades > 0 ? wins / trades * 100 : finite(readTrackStat("win_rate", "win_pct"));
  const avgR = finite(readTrackStat("avg_r", "mean_r"));
  const equity = finite(paper.equity);
  const rules = [
    ["완료 거래", `${Math.round(trades).toLocaleString("ko-KR")}건`],
    ["승리 거래", `${Math.round(wins).toLocaleString("ko-KR")}건`],
    ["비중 위반", `${(paper.cap_violations || []).length}건`],
    ["규칙 위반", `${(paper.rule_violations || []).length}건`],
  ];
  content.innerHTML = `
    <div class="performance-grid">
      <div class="performance-card"><small>누적 수익률</small><strong class="${ret >= 0 ? "gain" : "loss"}">${formatPercent(ret)}</strong><p>자동 모의투자 기준</p></div>
      <div class="performance-card"><small>승률</small><strong>${winRate.toFixed(1)}%</strong><p>종료된 거래 기준</p></div>
      <div class="performance-card"><small>평균 R</small><strong>${avgR.toFixed(2)}R</strong><p>포워드 테스트 집계</p></div>
      <div class="performance-card"><small>평가 자산</small><strong>${formatPrice(equity, "KRW")}</strong><p>실제 계좌가 아닌 모의자산</p></div>
      <div class="performance-card"><small>현금</small><strong>${formatPrice(paper.cash, "KRW")}</strong><p>미체결 주문 반영 전후는 원본 기준</p></div>
      <div class="performance-card"><small>업데이트</small><strong>${relativeMinutes(paper.updated) ?? "—"}분 전</strong><p>성과 JSON 발행 시각</p></div>
      <div class="performance-card performance-wide">
        <small>운영 규칙 상태</small>
        <div class="rule-list">${rules.map(([label, value]) => `<div class="rule-row"><span>${label}</span><strong>${value}</strong></div>`).join("")}</div>
      </div>
    </div>`;
}

function renderLoading() {
  content.innerHTML = `<div class="loading-grid" aria-label="데이터 로딩 중">
    <div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>
  </div>`;
}

function render() {
  updateHero();
  if (state.loading) {
    renderLoading();
    return;
  }
  if (state.view === "briefing") renderBriefing();
  else if (VIEW_META[state.view].group) renderSignals();
  else if (state.view === "portfolio") renderPortfolio();
  else renderPerformance();
  $("[data-retry]", content)?.addEventListener("click", () => loadData());
}

function initializeControls() {
  $(".brand").addEventListener("click", (event) => {
    event.preventDefault();
    setView("briefing");
  });
  $$("[data-view]").forEach((button) =>
    button.addEventListener("click", () => setView(button.dataset.view)));
  $$(".filter-chip").forEach((button) => {
    button.addEventListener("click", () => {
      state.market = button.dataset.market;
      $$(".filter-chip").forEach((item) => item.classList.toggle("active", item === button));
      render();
    });
  });
  $("#search-input").addEventListener("input", (event) => {
    state.query = event.target.value.trim();
    render();
  });
  $("#sort-select").addEventListener("change", (event) => {
    state.sort = event.target.value;
    render();
  });
  $("#refresh-button").addEventListener("click", () => loadData());
  $("#theme-button").addEventListener("click", () => {
    const root = document.documentElement;
    const next = root.dataset.theme === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    localStorage.setItem("flow-theme", next);
    requestAnimationFrame(() => {
      redrawChart($("#price-chart:not(.hidden)"));
      redrawChart($("#performance-chart"));
    });
  });
  $("#dialog-close").addEventListener("click", () => {
    dialog.close();
    delete dialog.dataset.position;
  });
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) {
      dialog.close();
      delete dialog.dataset.position;
    }
  });
  window.addEventListener("resize", () => {
    redrawChart($("#price-chart:not(.hidden)"));
    redrawChart($("#performance-chart"));
  });
}

function boot() {
  const savedTheme = localStorage.getItem("flow-theme");
  if (savedTheme === "dark" || savedTheme === "light") {
    document.documentElement.dataset.theme = savedTheme;
  } else if (window.matchMedia("(prefers-color-scheme: dark)").matches) {
    document.documentElement.dataset.theme = "dark";
  }
  initializeControls();
  const hashView = location.hash.replace("#", "");
  setView(VIEW_META[hashView] ? hashView : "briefing", { updateHash: false });
  loadData().finally(schedulePortfolioRefresh);
  setInterval(() => loadData({ quiet: true }), 60_000);
}

boot();
