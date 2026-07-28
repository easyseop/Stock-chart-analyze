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
  });
});
