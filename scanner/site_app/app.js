"use strict";

const API = Object.freeze({
  signals: "../api/signals.json",
  paper: "../api/paper_auto.json",
  track: "../api/track.json",
  portfolio: "../api/portfolio.json",
  chart: "../api/chart.json",
  quotes: "../api/quotes.json",
  performance: "../api/performance.json",
});

const VIEW_META = Object.freeze({
  now: { eyebrow: "STRATEGY A · REVERSAL", title: "전략 A · 진입 후보", group: "now" },
  watch: { eyebrow: "STRATEGY A · WATCH", title: "전략 A · 관찰 후보", group: "watch" },
  shelf: { eyebrow: "STRATEGY B", title: "매물대 반등 후보", group: "shelf" },
  portfolio: { eyebrow: "PRIVATE · READ ONLY", title: "내 자산" },
  performance: { eyebrow: "KIS vs MARKET", title: "성과 · 지수 비교" },
});

const state = {
  view: "now",
  market: "all",
  query: "",
  sort: "stage",
  signalsDoc: null,
  paper: null,
  track: null,
  portfolio: null,
  performance: null,
  performanceMarket: "US",
  performanceRange: "today",
  publicError: null,
  portfolioError: null,
  performanceError: null,
  loading: true,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const content = $("#content");
const dialog = $("#detail-dialog");
let portfolioTimer = null;

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

function marketLabel(ccy) {
  return ccy === "KRW" ? "KR · 원화" : "US · 달러";
}

function groupSignals(group) {
  return (state.signalsDoc?.signals || []).filter((signal) => signal.group === group);
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
  updateFreshness();
  if (state.view === "portfolio") {
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

  if (state.view === "portfolio" && state.portfolio) {
    const seconds = Number(state.portfolio.price_age_seconds);
    freshness.className = `freshness ${Number.isFinite(seconds) && seconds <= 90 ? "fresh" : "neutral"}`;
    freshness.textContent = Number.isFinite(seconds)
      ? `${Math.max(0, Math.round(seconds))}초 전 시세`
      : "KIS 잔고 조회";
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

  if (VIEW_META[state.view].group) {
    const items = groupSignals(VIEW_META[state.view].group);
    number.textContent = items.length.toLocaleString("ko-KR");
    unit.textContent = "종목";
    kicker.textContent = state.view === "now" ? "전략 A · 전환 확인" :
      state.view === "watch" ? "전략 A · 전환 대기" : "전략 B · 매물대 반등";
    description.textContent = state.view === "now"
      ? "하락에서 상승으로 전환이 확인된 전략 A의 실제 KIS 진입 후보입니다."
      : state.view === "watch"
        ? "전략 A 후보 중 아직 매수 조건을 완전히 충족하지 않은 관찰 종목입니다."
        : "전략 B로 분류된 매물대 반등 후보를 원본 값 그대로 표시합니다.";
  } else if (state.view === "portfolio") {
    const positions = state.portfolio?.positions || [];
    number.textContent = positions.length.toLocaleString("ko-KR");
    unit.textContent = "보유";
    kicker.textContent = state.portfolio ? "KIS 계좌 · 읽기 전용" : "내 컴퓨터에서만";
    description.textContent = state.portfolio
      ? "주문 기능과 분리된 조회 전용 연결입니다. 정보는 이 브라우저 밖으로 공개되지 않아요."
      : "실제 보유 정보는 공개 사이트에 표시하지 않습니다. 로컬 대시보드를 실행하면 이곳에서 확인할 수 있어요.";
  } else {
    const market = state.performance?.markets?.[state.performanceMarket];
    const latest = market?.series?.at(-1);
    const returnValue = latest ? finite(latest.account) : finite(state.paper?.ret_pct);
    number.textContent = formatPercent(returnValue);
    unit.textContent = "";
    kicker.textContent = latest
      ? `${market.label} KIS 보유 평가 · 지수와 같은 시작점`
      : "모의투자 누적 수익률";
    description.textContent = latest
      ? "전략 A·B와 시장지수를 모두 0%에서 시작해 상대 성과를 비교합니다."
      : "지수 비교 데이터가 쌓이기 전에는 기존 모의투자 성과를 표시합니다.";
  }
}

function filteredSignals() {
  const group = VIEW_META[state.view].group;
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

function signalCard(signal) {
  const tactic = signal.tactic || {};
  const badges = [
    signal.fresh ? `<span class="badge new">NEW</span>` : "",
    signal.group === "now" ? `<span class="badge strategy-a">전략 A</span>` : "",
    signal.group === "watch" ? `<span class="badge strategy-a">A 관찰</span>` : "",
    tactic.label ? `<span class="badge tactic">${escapeHTML(tactic.label)}</span>` : "",
    signal.group === "shelf" ? `<span class="badge shelf">전략 B</span>` : "",
  ].join("");
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
        <span class="metric"><small>진입</small><b>${formatPrice(signal.entry, signal.ccy)}</b></span>
        <span class="metric stop"><small>손절</small><b>${formatPrice(signal.stop, signal.ccy)}</b></span>
        <span class="metric target"><small>목표</small><b>${formatPrice(signal.target, signal.ccy)}</b></span>
      </span>
      <span class="stage-line">
        <small>단계 ${Math.round(finite(signal.stage))}/4</small>
        <span class="stage-track">${stageBars(signal.stage)}</span>
      </span>
    </button>`;
}

function renderSignals() {
  if (state.publicError || !state.signalsDoc) {
    content.innerHTML = errorState("신호를 불러오지 못했어요", "공개 데이터 연결을 확인한 뒤 다시 시도해 주세요.");
    return;
  }
  const items = filteredSignals();
  if (!items.length) {
    const hasGroup = groupSignals(VIEW_META[state.view].group).length > 0;
    content.innerHTML = emptyState(
      hasGroup ? "검색 조건에 맞는 종목이 없어요" : "오늘은 해당 신호가 없어요",
      hasGroup ? "필터나 검색어를 바꾸면 다른 종목을 볼 수 있어요." :
        "신호가 0건인 날도 정상입니다. 조건을 낮추거나 임의 종목을 만들지 않습니다.");
    return;
  }
  content.innerHTML = `<div class="signal-grid">${items.map(signalCard).join("")}</div>`;
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
  $("#detail-content").innerHTML = `
    <div class="detail-price">${formatPrice(signal.price, signal.ccy)}</div>
    <div class="detail-grid">
      <div class="detail-box"><small>제안 진입가</small><strong>${formatPrice(signal.entry, signal.ccy)}</strong></div>
      <div class="detail-box"><small>손절가</small><strong class="loss">${formatPrice(signal.stop, signal.ccy)}</strong></div>
      <div class="detail-box"><small>목표가</small><strong class="gain">${formatPrice(signal.target, signal.ccy)}</strong></div>
    </div>
    <div class="tactic-box">
      <strong>${escapeHTML(tactic.label || "진입 전술")}</strong>
      <p>${escapeHTML(tactic.desc || "원본 신호에 전술 설명이 없습니다.")}</p>
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
  return `<div class="shelf-box">
    <h3>매물대 정보</h3>
    <div class="shelf-values">
      <div><small>POC</small><b>${formatPrice(shelf.poc, ccy)}</b></div>
      <div><small>VAL</small><b>${formatPrice(shelf.val, ccy)}</b></div>
      <div><small>VAH</small><b>${formatPrice(shelf.vah, ccy)}</b></div>
      <div><small>손익비</small><b>${finite(shelf.rr).toFixed(2)}R</b></div>
    </div>
  </div>`;
}

function portfolioTotals(positions) {
  const totals = {};
  positions.forEach((position) => {
    const row = totals[position.ccy] ||= { eval: 0, buy: 0, pl: 0 };
    row.eval += finite(position.eval_amt);
    row.buy += finite(position.buy_amt);
    row.pl += finite(position.pl_amt);
  });
  return totals;
}

function portfolioCard(position) {
  const tone = finite(position.pl_amt) >= 0 ? "gain" : "loss";
  const sleeve = String(position.sleeve || "A").toUpperCase() === "B" ? "B" : "A";
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
        <strong>${formatPrice(position.eval_amt, position.ccy)}</strong>
        <span class="${tone}">${formatPercent(position.pl_rt)}</span>
      </span>
      <span class="portfolio-meta">
        ${Number(position.qty).toLocaleString("ko-KR")}주 · 평균 ${formatPrice(position.avg, position.ccy)}
        · 손익 ${formatPrice(position.pl_amt, position.ccy, true)}
      </span>
    </button>`;
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
  if (!positions.length) {
    content.innerHTML = emptyState("현재 보유 종목이 없어요", "연결된 KIS 환경의 보유 잔고가 0건입니다.");
    return;
  }
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
    <div class="portfolio-summary">${summary}</div>
    <div class="portfolio-grid">${positions.map(portfolioCard).join("")}</div>`;
  $$(".portfolio-card", content).forEach((card) => {
    card.addEventListener("click", () => {
      const position = positions.find((item) => item.code === card.dataset.position);
      if (position) openPortfolioDetail(position);
    });
  });
}

function openPortfolioDetail(position) {
  $("#detail-market").textContent = `${marketLabel(position.ccy)} · ${position.code}`;
  $("#detail-title").textContent = position.name || position.code;
  const tone = finite(position.pl_amt) >= 0 ? "gain" : "loss";
  const sleeve = String(position.sleeve || "A").toUpperCase() === "B" ? "B" : "A";
  dialog.dataset.position = position.code;
  dialog.dataset.chartMode = isPrivateDashboard() ? "live" : "daily";
  dialog.dataset.chartDays = "90";
  $("#detail-content").innerHTML = `
    <div class="detail-price-row">
      <div class="detail-price" id="detail-live-price">${formatPrice(position.cur, position.ccy)}</div>
      <span class="badge ${sleeve === "B" ? "shelf" : "strategy-a"}">전략 ${sleeve}</span>
    </div>
    <div class="detail-grid">
      <div class="detail-box"><small>보유 수량</small><strong>${Number(position.qty).toLocaleString("ko-KR")}주</strong></div>
      <div class="detail-box"><small>평균 단가</small><strong>${formatPrice(position.avg, position.ccy)}</strong></div>
      <div class="detail-box"><small>평가 손익</small><strong id="detail-live-pl" class="${tone}">${formatPercent(position.pl_rt)}</strong></div>
    </div>
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

function refreshOpenPortfolioDetail() {
  if (!dialog.open || !dialog.dataset.position) return;
  const position = (state.portfolio?.positions || []).find(
    (item) => item.code === dialog.dataset.position);
  if (!position) return;
  const price = $("#detail-live-price");
  const pl = $("#detail-live-pl");
  if (price) price.textContent = formatPrice(position.cur, position.ccy);
  if (pl) {
    pl.textContent = formatPercent(position.pl_rt);
    pl.className = finite(position.pl_amt) >= 0 ? "gain" : "loss";
  }
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
        ["현재가", "blue"], ["평균매수가", "violet"],
        ["손절", "red"], ["목표", "green"],
      ]);
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
        ["상승", "green"], ["하락", "red"], ["MA20", "blue"],
        ["MA60", "amber"], ["MA120", "violet"],
      ]);
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

function chartLegend(items) {
  return items.map(([label, tone]) =>
    `<span><i class="legend-${tone}"></i>${escapeHTML(label)}</span>`).join("");
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

function chartReferenceLines(ctx, colors, position, y, left, rightX) {
  const lines = [
    ["평균", finite(position.avg), colors.violet],
    ["손절", finite(position.stop), colors.red],
    ["목표", finite(position.target), colors.green],
  ].filter(([, value]) => value > 0);
  ctx.font = "10px -apple-system, sans-serif";
  lines.forEach(([label, value, lineColor]) => {
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
  chartReferenceLines(ctx, colors, position, y, pad.left, width - pad.right);
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
  drawSeries(ctx, points, "ma20", colors.blue, x, y);
  drawSeries(ctx, points, "ma60", colors.amber, x, y);
  drawSeries(ctx, points, "ma120", colors.violet, x, y);
  chartReferenceLines(ctx, colors, position, y, pad.left, width - pad.right);
  if (finite(position.cur) > 0) {
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
    return (marketDoc.series || []).map((point) => ({
      label: point.t,
      account: point.account,
      A: point.A,
      B: point.B,
      ...Object.fromEntries(indexNames.map((name) =>
        [`idx:${name}`, point.indices?.[name]])),
    }));
  }
  const limit = range === "1m" ? 30 : range === "3m" ? 90 : 10000;
  const daily = (state.performance?.days || [])
    .filter((row) => row.market === market)
    .slice(-limit)
    .map((row) => ({
      label: row.date?.slice(5) || "—",
      account: row.account,
      A: row.A,
      B: row.B,
      ...Object.fromEntries(indexNames.map((name) =>
        [`idx:${name}`, row.indices?.[name]])),
    }));
  const current = marketDoc.series?.at(-1);
  if (current && marketDoc.date && !daily.some((row) => row.label === marketDoc.date.slice(5))) {
    daily.push({
      label: marketDoc.date.slice(5),
      account: current.account,
      A: current.A,
      B: current.B,
      ...Object.fromEntries(indexNames.map((name) =>
        [`idx:${name}`, current.indices?.[name]])),
    });
  }
  const keys = ["account", "A", "B", ...indexNames.map((name) => `idx:${name}`)];
  const cumulative = Object.fromEntries(keys.map((key) => [key, 0]));
  return daily.map((row) => {
    const out = { label: row.label };
    keys.forEach((key) => {
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
    return out;
  });
}

function performanceValue(value) {
  return value !== null && value !== undefined && Number.isFinite(Number(value))
    ? formatPercent(Number(value), 2) : "—";
}

function renderKisPerformance() {
  const market = state.performanceMarket;
  const marketDoc = state.performance?.markets?.[market] || {};
  const range = state.performanceRange;
  const rows = performanceRows(market, range);
  const latest = rows.at(-1) || {};
  const indexNames = marketDoc.indices || [];
  const primary = indexNames[0];
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
        <div class="performance-card"><small>KIS 보유 평가</small><strong class="${finite(latest.account) >= 0 ? "gain" : "loss"}">${performanceValue(latest.account)}</strong><p>${range === "today" ? "장 시작" : "선택 기간"} 대비</p></div>
        <div class="performance-card"><small>전략 A · 전환</small><strong>${performanceValue(latest.A)}</strong><p>A 보유 종목 기준</p></div>
        <div class="performance-card"><small>전략 B · 매물대</small><strong>${performanceValue(latest.B)}</strong><p>B 보유 종목 기준</p></div>
        <div class="performance-card"><small>${escapeHTML(primary || "주 지수")} 대비</small><strong class="${finite(alphaValue) >= 0 ? "gain" : "loss"}">${performanceValue(alphaValue)}</strong><p>초과수익률(%p)</p></div>
      </div>
      <div class="performance-chart-card">
        <div class="chart-head">
          <div><strong>${escapeHTML(marketDoc.label || market)} 시장 비교</strong><span>모든 선을 같은 0% 기준으로 비교</span></div>
          <span class="live-pill">${age === null ? "기록 중" : age === 0 ? "방금 갱신" : `${age}분 전`}</span>
        </div>
        <canvas id="performance-chart" role="img" aria-label="KIS 전략과 시장지수 수익률 비교 차트"></canvas>
        <div class="chart-legend">
          <span><i class="legend-blue"></i>KIS 전체</span>
          <span><i class="legend-green"></i>전략 A</span>
          <span><i class="legend-violet"></i>전략 B</span>
          ${indexNames.map((name, index) => `<span><i class="${index ? "legend-amber" : "legend-muted"}"></i>${escapeHTML(name)}</span>`).join("")}
        </div>
        <p class="chart-note">${escapeHTML(state.performance?.basis || "KIS 봇 보유 평가손익 기준")} · ${finite(state.performance?.sample_seconds, 300) / 60}분 간격. 과거가 없는 지수는 지금부터 쌓입니다.</p>
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
  const canvas = $("#performance-chart");
  if (canvas && rows.length) {
    const keys = ["account", "A", "B", ...indexNames.map((name) => `idx:${name}`)];
    const labels = {
      account: "KIS 전체", A: "전략 A", B: "전략 B",
      ...Object.fromEntries(indexNames.map((name) => [`idx:${name}`, name])),
    };
    requestAnimationFrame(() => drawPerformanceChart(canvas, rows, keys, labels));
  }
}

function drawPerformanceChart(canvas, rows, keys, labels) {
  canvas._chartData = { kind: "performance", rows, keys, labels };
  const { ctx, colors, width, height } = chartCanvas(canvas, 340);
  const pad = { left: 18, right: 48, top: 20, bottom: 28 };
  const values = rows.flatMap((row) => keys.flatMap((key) =>
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
  let indexNo = 0;
  keys.forEach((key) => {
    const color = palette[key] || (indexNo++ ? colors.amber : colors.muted);
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
  if (VIEW_META[state.view].group) renderSignals();
  else if (state.view === "portfolio") renderPortfolio();
  else renderPerformance();
  $("[data-retry]", content)?.addEventListener("click", () => loadData());
}

function initializeControls() {
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
  setView(VIEW_META[hashView] ? hashView : "now", { updateHash: false });
  loadData().finally(schedulePortfolioRefresh);
  setInterval(() => loadData({ quiet: true }), 60_000);
}

boot();
