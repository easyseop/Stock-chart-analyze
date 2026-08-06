"use strict";

(function exposePortfolioMath(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.PortfolioMath = api;
})(typeof globalThis !== "undefined" ? globalThis : this, () => {
  function optionalNumber(value) {
    if (value === null || value === undefined || value === "") return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function finiteNumber(value, fallback = 0) {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  }

  function positiveNumber(value) {
    const number = optionalNumber(value);
    return number !== null && number > 0 ? number : null;
  }

  function positionInvestmentSummary(position) {
    const quantity = positiveNumber(position.qty);
    const averagePrice = positiveNumber(position.avg);
    const currentPrice = positiveNumber(position.cur);
    const brokerInvested = positiveNumber(position.buy_amt);
    const brokerValue = positiveNumber(position.eval_amt);
    const brokerPnl = optionalNumber(position.pl_amt);
    const brokerReturn = optionalNumber(position.pl_rt);
    const investedAmount = brokerInvested
      ?? (quantity !== null && averagePrice !== null ? quantity * averagePrice : null);
    const currentValue = brokerValue
      ?? (quantity !== null && currentPrice !== null ? quantity * currentPrice : null);
    const pnlAmount = brokerPnl
      ?? (investedAmount !== null && currentValue !== null
        ? currentValue - investedAmount : null);
    const returnPct = brokerReturn
      ?? (investedAmount > 0 && pnlAmount !== null ? pnlAmount / investedAmount * 100 : null);
    const perSharePnl = averagePrice !== null && currentPrice !== null
      ? currentPrice - averagePrice : null;
    const priceGapPct = averagePrice !== null && currentPrice !== null
      ? (currentPrice / averagePrice - 1) * 100 : null;
    const breakEvenMovePct = averagePrice !== null && currentPrice !== null
      && currentPrice < averagePrice
      ? (averagePrice / currentPrice - 1) * 100 : 0;
    return {
      quantity,
      averagePrice,
      currentPrice,
      investedAmount,
      currentValue,
      pnlAmount,
      returnPct,
      perSharePnl,
      priceGapPct,
      breakEvenMovePct,
    };
  }

  function holdingPeriod(opened, today = "") {
    const openedText = String(opened || "").trim();
    if (!/^\d{4}-\d{2}-\d{2}$/.test(openedText)) return null;
    const openedMs = Date.parse(`${openedText}T00:00:00Z`);
    if (!Number.isFinite(openedMs)
        || new Date(openedMs).toISOString().slice(0, 10) !== openedText) return null;
    let todayText = String(today || "").trim();
    if (!todayText) {
      const now = new Date();
      todayText = [
        now.getFullYear(),
        String(now.getMonth() + 1).padStart(2, "0"),
        String(now.getDate()).padStart(2, "0"),
      ].join("-");
    }
    if (!/^\d{4}-\d{2}-\d{2}$/.test(todayText)) return null;
    const todayMs = Date.parse(`${todayText}T00:00:00Z`);
    if (!Number.isFinite(todayMs)
        || new Date(todayMs).toISOString().slice(0, 10) !== todayText
        || openedMs > todayMs) return null;
    return {
      opened: openedText,
      holdingDays: Math.floor((todayMs - openedMs) / 86400000) + 1,
    };
  }

  function positionDistances(position) {
    const current = optionalNumber(position.cur);
    const stop = optionalNumber(position.stop);
    const target = optionalNumber(position.target);
    if (!(current > 0)) return { stopPct: null, targetPct: null, progress: null };
    const stopPct = stop > 0 ? (current - stop) / current * 100 : null;
    const targetPct = target > 0 ? (target - current) / current * 100 : null;
    const progress = stop > 0 && target > stop
      ? Math.max(0, Math.min(100, (current - stop) / (target - stop) * 100))
      : null;
    return { stopPct, targetPct, progress };
  }

  function strategyStats(positions) {
    const result = {
      A: { count: 0, markets: {} },
      B: { count: 0, markets: {} },
    };
    positions.forEach((position) => {
      const sleeve = String(position.sleeve || "A").toUpperCase() === "B" ? "B" : "A";
      const row = result[sleeve];
      row.count += 1;
      const market = row.markets[position.ccy] ||= { buy: 0, pl: 0 };
      market.buy += finiteNumber(position.buy_amt);
      market.pl += finiteNumber(position.pl_amt);
    });
    return result;
  }

  function concentrationRows(positions) {
    const markets = {};
    positions.forEach((position) => {
      const group = markets[position.ccy] ||= { total: 0, positions: [] };
      group.total += finiteNumber(position.eval_amt);
      group.positions.push(position);
    });
    return Object.entries(markets).map(([ccy, group]) => {
      const largest = [...group.positions].sort(
        (a, b) => finiteNumber(b.eval_amt) - finiteNumber(a.eval_amt))[0];
      const weight = group.total > 0
        ? finiteNumber(largest?.eval_amt) / group.total * 100
        : 0;
      return {
        ccy,
        name: largest?.name || largest?.code || "—",
        weight,
        tone: weight >= 50 ? "warning" : "neutral",
      };
    });
  }

  function maximumDrawdown(rows, key = "account") {
    // 성과 시계열은 세션 시작 누적수익률 0%(wealth=1)를 기준점으로 삼는다.
    let peak = 1;
    let worst = 0;
    let samples = 0;
    rows.forEach((row) => {
      const value = optionalNumber(row[key]);
      if (value === null) return;
      const wealth = 1 + value / 100;
      peak = Math.max(peak, wealth);
      if (peak > 0) worst = Math.min(worst, (wealth / peak - 1) * 100);
      samples += 1;
    });
    return samples >= 2 ? worst : null;
  }

  function completeHoldingsValue(holdings) {
    const value = optionalNumber(holdings?.account);
    const covered = Math.max(0, Math.trunc(finiteNumber(holdings?.covered)));
    const eligible = Math.max(0, Math.trunc(finiteNumber(holdings?.eligible)));
    return {
      value: value !== null && eligible > 0 && covered === eligible ? value : null,
      covered,
      eligible,
      complete: value !== null && eligible > 0 && covered === eligible,
    };
  }

  function completeHoldingsSeries(rows) {
    const samples = (Array.isArray(rows) ? rows : []).map(completeHoldingsValue);
    const incompleteDays = samples.filter((sample) => !sample.complete).length;
    return {
      complete: samples.length > 0 && incompleteDays === 0,
      samples: samples.length,
      incompleteDays,
    };
  }

  // ── 성과 vs 지수 순수 계산(perf.html·app.js 공용) ──────────────
  // null(미확정)은 어떤 산술에도 0처럼 넣지 않는다 — JS에서 `x - null`은
  // 조용히 `x - 0`이 된다(Codex TWR-V4 P1-1). 아래 함수만 통해서 계산한다.

  function todayIndexDiff(account, index) {
    const acct = optionalNumber(account);
    const idx = optionalNumber(index);
    return acct === null || idx === null ? null : acct - idx;
  }

  function cumulativeAlphaSeries(rows) {
    // 계좌·지수를 **각각 복리**한 뒤의 차이(%p). 어느 한쪽이라도 미확정/결측인
    // 날부터는 잇지 않는다 — 끊긴 구간을 하나의 연속 곡선처럼 복리 금지.
    let accountWealth = 1;
    let indexWealth = 1;
    let broken = false;
    return rows.map((row) => {
      const acct = optionalNumber(row.acct);
      const idx = optionalNumber(row.idx);
      if (broken || acct === null || idx === null) {
        broken = true;
        return null;
      }
      accountWealth *= 1 + acct / 100;
      indexWealth *= 1 + idx / 100;
      return Number(((accountWealth - indexWealth) * 100).toFixed(2));
    });
  }

  function dailyIndexValue(row, name) {
    // 일간 지수 수익률 선택. `daily_indices` 키가 있는 새 스키마 행에서는
    // **명시적 null(미확정)을 세션 기준값으로 폴백하지 않는다** — 기준이 다른
    // 값을 일간 수익률로 쓰면 모르는 지수가 숫자로 둔갑한다(TWR-V4 P1-2).
    // 키 자체가 없는 구버전 행만 세션 값 폴백을 허용한다.
    const daily = row ? row.daily_indices : null;
    if (daily && typeof daily === "object") {
      return Object.prototype.hasOwnProperty.call(daily, name)
        ? optionalNumber(daily[name])
        : null;
    }
    if (daily === undefined || daily === null) {
      const session = row && row.indices;
      return session && typeof session === "object"
        ? optionalNumber(session[name])
        : null;
    }
    return null;
  }

  function incompleteCount(rows, key) {
    return rows.filter((row) => optionalNumber(row[key]) === null).length;
  }

  function compoundComplete(values) {
    if (!Array.isArray(values) || !values.length) return null;
    let wealth = 1;
    for (const raw of values) {
      const value = optionalNumber(raw);
      if (value === null || value <= -100) return null;
      wealth *= 1 + value / 100;
    }
    return (wealth - 1) * 100;
  }

  function monthlyPerformance(rows, indexNames = []) {
    const groups = new Map();
    (Array.isArray(rows) ? rows : []).forEach((row) => {
      const date = String(row?.date || "");
      if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return;
      const month = date.slice(0, 7);
      if (!groups.has(month)) groups.set(month, []);
      groups.get(month).push(row);
    });
    return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b))
      .map(([month, samples]) => {
        const indices = Object.fromEntries(indexNames.map((name) => [
          name, compoundComplete(samples.map((row) => dailyIndexValue(row, name))),
        ]));
        return {
          month,
          days: samples.length,
          account: compoundComplete(samples.map((row) => row.account)),
          A: compoundComplete(samples.map((row) => row.A)),
          B: compoundComplete(samples.map((row) => row.B)),
          indices,
        };
      });
  }

  function tradeMonth(row) {
    const day = String(row?.day || "");
    if (/^\d{4}-\d{2}-\d{2}$/.test(day)) return day.slice(0, 7);
    const executed = String(row?.executed_at || "");
    return /^\d{4}-\d{2}/.test(executed) ? executed.slice(0, 7) : "";
  }

  function monthlyTradeStats(trades, market) {
    const groups = new Map();
    (Array.isArray(trades) ? trades : []).forEach((row) => {
      if (String(row?.side || "").toLowerCase() !== "sell"
          || String(row?.market || "").toUpperCase() !== String(market || "").toUpperCase()) {
        return;
      }
      const month = tradeMonth(row);
      if (!month) return;
      if (!groups.has(month)) groups.set(month, []);
      groups.get(month).push(row);
    });
    return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b))
      .map(([month, sales]) => {
        const realized = sales.map((row) => optionalNumber(row.realized_pnl_krw));
        const costs = sales.map((row) => optionalNumber(row.cost_closed_krw));
        const completePnl = realized.every((value) => value !== null);
        const completeCost = costs.every((value) => value !== null && value > 0);
        const pnl = completePnl ? realized.reduce((sum, value) => sum + value, 0) : null;
        const cost = completeCost ? costs.reduce((sum, value) => sum + value, 0) : null;
        const wins = realized.filter((value) => value !== null && value > 0).length;
        const losses = realized.filter((value) => value !== null && value < 0).length;
        const decided = wins + losses;
        return {
          month,
          exits: sales.length,
          wins,
          losses,
          winRate: decided ? wins / decided * 100 : null,
          realizedPnlKrw: pnl,
          costClosedKrw: cost,
          realizedReturnPct: pnl !== null && cost > 0 ? pnl / cost * 100 : null,
          estimatedCount: sales.filter((row) => row.price_estimated === true).length,
          complete: completePnl && completeCost,
        };
      });
  }

  function strategyTradeStats(trades, sleeve, market = "") {
    const sales = (Array.isArray(trades) ? trades : []).filter((row) =>
      String(row?.side || "").toLowerCase() === "sell"
      && String(row?.sleeve || "A").toUpperCase() === String(sleeve || "A").toUpperCase()
      && (!market || String(row?.market || "").toUpperCase() === String(market).toUpperCase()));
    const pnl = sales.map((row) => optionalNumber(row.realized_pnl_krw));
    const wins = pnl.filter((value) => value !== null && value > 0).length;
    const losses = pnl.filter((value) => value !== null && value < 0).length;
    const decided = wins + losses;
    return {
      exits: sales.length,
      wins,
      losses,
      winRate: decided ? wins / decided * 100 : null,
      estimatedCount: sales.filter((row) => row.price_estimated === true).length,
      realizedPnlKrw: pnl.every((value) => value !== null)
        ? pnl.reduce((sum, value) => sum + value, 0) : null,
    };
  }

  function unrealizedSummary(positions, market) {
    const selected = (Array.isArray(positions) ? positions : []).filter((row) => {
      const rowMarket = String(row?.market || "").toUpperCase()
        || (String(row?.ccy || "").toUpperCase() === "KRW" ? "KR" : "US");
      return rowMarket === String(market || "").toUpperCase();
    });
    const buys = selected.map((row) => optionalNumber(row.buy_amt));
    const pnls = selected.map((row) => optionalNumber(row.pl_amt));
    const complete = selected.length > 0
      && buys.every((value) => value !== null && value >= 0)
      && pnls.every((value) => value !== null);
    const invested = complete ? buys.reduce((sum, value) => sum + value, 0) : null;
    const pnl = complete ? pnls.reduce((sum, value) => sum + value, 0) : null;
    return {
      count: selected.length,
      invested,
      pnl,
      returnPct: invested > 0 ? pnl / invested * 100 : null,
      complete,
    };
  }

  return Object.freeze({
    optionalNumber,
    positionInvestmentSummary,
    holdingPeriod,
    positionDistances,
    strategyStats,
    concentrationRows,
    maximumDrawdown,
    completeHoldingsValue,
    completeHoldingsSeries,
    todayIndexDiff,
    cumulativeAlphaSeries,
    dailyIndexValue,
    incompleteCount,
    compoundComplete,
    monthlyPerformance,
    monthlyTradeStats,
    strategyTradeStats,
    unrealizedSummary,
  });
});
