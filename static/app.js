/* Dashboard renderer.
 *
 * Polls /api/snapshot and repaints. No framework, no build step: this ships with
 * a Python bot whose dependency list is four lines long, and a bundler would cost
 * more than it saves for ~300 lines of DOM writes.
 *
 * Every value rendered here is read-only. The kill switch button displays
 * config.TRADING_HALTED; it cannot change it. Halting trading requires editing
 * config.py and restarting the bot, which is friction on purpose.
 */

const POLL_MS = 5000;

// The limits are duplicated from config.py so the gauges can render their
// thresholds before the first snapshot lands. If they drift from config, the
// numbers are still correct -- the authoritative check is the guardrail panel,
// which reports the engine's own verdict.
const LIMITS = { drawdown: 0.03, leverage: 2.0 };

const $ = (id) => document.getElementById(id);

// Clamp before formatting a percentage: a negative drawdown (day is up) is
// meaningful, a 4000% one is a broken snapshot and must not render as a width.
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const pct = (v, d = 2) => `${(v * 100).toFixed(d)}%`;
const usd = (v) =>
  v.toLocaleString("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2 });

function timeOf(iso) {
  if (!iso) return "--:--:--";
  return new Date(iso).toLocaleTimeString("en-GB", { hour12: false });
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

/* ------------------------------------------------------------- top bar -- */
function renderHeader(s) {
  const account = s.account;

  $("m-symbol").textContent = `${s.exchange} · ${s.symbol} · ${s.timeframe || "1d"}`;

  $("m-dd-max").textContent = pct(LIMITS.drawdown, 1);
  $("m-lev-max").textContent = `${LIMITS.leverage.toFixed(2)}x max`;

  $("m-paper").textContent = s.paper_trading ? "PAPER" : "LIVE";
  $("m-paper").className = `pill ${s.paper_trading ? "pill-warn" : "pill-bad"}`;

  if (!account) return;

  const equity = account.total_equity;
  $("m-equity").textContent = usd(equity);
  $("m-equity-sub").textContent = `${usd(account.cash_balance)} cash · ${usd(account.current_position_value)} position`;

  // starting_daily_equity is set to current equity at snapshot time, so P&L is
  // 0 until the day genuinely rolls over. It is still the correct comparison
  // point once a second snapshot on the same day has a different value.
  const start = account.starting_daily_equity || equity;
  const pnl = equity - start;
  const pnlPct = start > 0 ? pnl / start : 0;
  const pnlNode = $("m-pnl");
  pnlNode.textContent = `${pnl >= 0 ? "+" : ""}${usd(pnl)}`;
  pnlNode.className = `metric-value num ${pnl > 0 ? "up" : pnl < 0 ? "down" : ""}`;
  $("m-pnl-sub").textContent = `${pnlPct >= 0 ? "+" : ""}${pct(pnlPct)} · since day start`;

  const dd = clamp((start - equity) / start, 0, 1);
  const ddFill = $("m-dd-fill");
  ddFill.style.width = `${clamp((dd / LIMITS.drawdown) * 100, 0, 100)}%`;
  ddFill.className = `gauge-fill ${dd >= LIMITS.drawdown ? "bad" : dd >= LIMITS.drawdown * 0.66 ? "warn" : ""}`;
  $("m-dd-val").textContent = pct(dd);
  $("m-dd-val").className = dd >= LIMITS.drawdown ? "down" : dd >= LIMITS.drawdown * 0.66 ? "warn" : "";

  const lev = equity > 0 ? account.current_position_value / equity : 0;
  const levFill = $("m-lev-fill");
  levFill.style.width = `${clamp((lev / LIMITS.leverage) * 100, 0, 100)}%`;
  levFill.className = `gauge-fill ${lev >= LIMITS.leverage ? "bad" : lev >= LIMITS.leverage * 0.66 ? "warn" : ""}`;
  $("m-lev-val").textContent = `${lev.toFixed(2)}x`;
}

function renderConnection(ok) {
  const node = $("m-conn");
  node.textContent = ok ? "LIVE FEED" : "STALE";
  node.className = `pill ${ok ? "pill-ok" : "pill-bad"}`;
}

function renderKillSwitch(halted) {
  const btn = $("kill");
  // Disabled: this page is read-only. The control surface is config.py.
  btn.disabled = true;
  $("kill-text").textContent = halted ? "TRADING HALTED" : "HALT ALL TRADING";
  btn.classList.toggle("halted", halted);
}

/* ------------------------------------------------------------ layer 1 -- */
function renderNews(items) {
  const feed = $("news-feed");
  feed.textContent = "";
  if (!items || !items.length) {
    feed.append(el("div", "empty", "no ingested documents"));
    return;
  }
  $("l1-status").textContent = `${items.length} DOCS`;
  $("l1-status").className = "pill pill-info";

  for (const item of items) {
    const row = el("div", "feed-item");
    const head = el("div", "feed-head");
    head.append(el("span", "feed-time", timeOf(item.published_at)));
    if (item.event_type) head.append(el("span", "pill pill-mute", item.event_type));
    if (item.ticker) head.append(el("span", "pill pill-info", item.ticker));
    row.append(head, el("div", "feed-title", item.headline || "(no headline)"));

    // The raw LLM object is shown verbatim. Hiding it would hide exactly the
    // thing an operator needs when a sentiment call looks wrong.
    if (item.extraction) {
      const pre = el("div", "feed-json");
      pre.textContent = JSON.stringify(item.extraction, null, 2);
      row.append(pre);
    }
    feed.append(row);
  }
}

function renderExtraction(ext) {
  const node = $("extraction");
  node.textContent = ext
    ? JSON.stringify(ext, null, 2)
    : '{\n  "status": "awaiting documents"\n}';
}

/* ------------------------------------------------------------ layer 2 -- */
function renderChart(candles) {
  const canvas = $("chart");
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 700;
  const cssH = 330;

  // Scale for the display density, otherwise every line renders blurry on HiDPI.
  canvas.width = cssW * dpr;
  canvas.height = cssH * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  if (!candles || candles.length < 2) {
    ctx.fillStyle = "#5c6470";
    ctx.font = "11px ui-monospace, monospace";
    ctx.fillText("no price data", 12, 24);
    $("chart-range").textContent = "no data";
    return;
  }

  const padL = 8, padR = 58, padT = 12, padB = 20;
  const w = cssW - padL - padR;
  const h = cssH - padT - padB;

  const highs = candles.map((c) => c.h);
  const lows = candles.map((c) => c.l);
  let hi = Math.max(...highs), lo = Math.min(...lows);

  // Fixed 4% band so candle bodies keep a consistent body-to-wick proportion.
  const span = hi - lo || hi * 0.01;
  hi += span * 0.04;
  lo -= span * 0.04;

  const x = (i) => padL + (i / (candles.length - 1)) * w;
  const y = (p) => padT + (1 - (p - lo) / (hi - lo)) * h;

  // Grid + price axis
  ctx.strokeStyle = "#1b2028";
  ctx.fillStyle = "#5c6470";
  ctx.font = "10px ui-monospace, monospace";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const gy = padT + (i / 4) * h;
    ctx.beginPath();
    ctx.moveTo(padL, gy);
    ctx.lineTo(padL + w, gy);
    ctx.stroke();
    ctx.fillText((hi - ((i / 4) * (hi - lo))).toPrecision(6), padL + w + 6, gy + 3);
  }

  // Candles. Bodies narrower as the series grows, so 120 bars stay legible.
  const step = w / candles.length;
  const bw = Math.max(1, Math.min(7, step * 0.62));
  candles.forEach((c, i) => {
    const up = c.c >= c.o;
    const color = up ? "#10b981" : "#ef4444";
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.moveTo(x(i), y(c.h));
    ctx.lineTo(x(i), y(c.l));
    ctx.stroke();
    const top = y(Math.max(c.o, c.c));
    const height = Math.max(1, Math.abs(y(c.o) - y(c.c)));
    ctx.fillRect(x(i) - bw / 2, top, bw, height);
  });

  // SMA 20 over the closes
  const period = 20;
  ctx.strokeStyle = "#06b6d4";
  ctx.lineWidth = 1.4;
  ctx.beginPath();
  let started = false;
  candles.forEach((c, i) => {
    if (i < period - 1) return;
    const slice = candles.slice(i - period + 1, i + 1);
    const avg = slice.reduce((a, k) => a + k.c, 0) / period;
    const px = x(i), py = y(avg);
    if (!started) { ctx.moveTo(px, py); started = true; } else { ctx.lineTo(px, py); }
  });
  ctx.stroke();

  // Signal marker on the most recent closed candle
  const last = candles[candles.length - 1];
  const smaNow = candles.slice(-period).reduce((a, k) => a + k.c, 0) / Math.min(period, candles.length);
  const bullish = last.c > smaNow;
  ctx.fillStyle = bullish ? "#10b981" : "#ef4444";
  ctx.beginPath();
  ctx.arc(x(candles.length - 1), y(last.c), 4, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = "#f59e0b";
  ctx.font = "600 10px ui-monospace, monospace";
  ctx.fillText(bullish ? "BUY" : "FLAT", padL + 4, padT + 12);

  const from = new Date(candles[0].t).toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
  const to = new Date(last.t).toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
  $("chart-range").textContent = `${from} → ${to} · ${candles.length}×${$("chart").dataset.tf || "1d"}`;
}

function renderSignals(signals) {
  const box = $("signal-stream");
  box.textContent = "";
  if (!signals || !signals.length) {
    box.append(el("div", "empty", "no active model signal"));
    $("l2-status").textContent = "NO SIGNAL";
    $("l2-status").className = "pill pill-mute";
    return;
  }

  $("l2-status").textContent = `${signals.length} ACTIVE`;
  $("l2-status").className = "pill pill-warn";

  for (const sig of signals) {
    const card = el("div");
    card.style.cssText = "border:1px solid var(--line);border-radius:4px;padding:11px;margin-bottom:10px;background:var(--surface-2)";

    const head = el("div");
    head.style.cssText = "display:flex;align-items:center;gap:9px;margin-bottom:9px";
    const dir = String(sig.action || "").toUpperCase();
    head.append(el("span", `pill ${dir === "BUY" ? "pill-ok" : dir === "SELL" ? "pill-bad" : "pill-mute"}`, dir || "?"));
    head.append(el("span", "", sig.ticker || ""));
    const spacer = el("div");
    spacer.style.flex = "1";
    head.append(spacer, el("span", "num", `${(Number(sig.conviction_score) * 100).toFixed(0)}% conviction`));
    card.append(head);

    const conv = Number(sig.conviction_score) || 0;
    const meter = el("div", "meter");
    meter.style.marginBottom = "9px";
    const fill = el("div", "meter-fill");
    fill.style.width = `${clamp(conv * 100, 0, 100)}%`;
    // Amber below the 0.70 gate the L3 engine enforces, emerald above it. This
    // makes a signal that is about to be rejected visually obvious beforehand.
    fill.className = `meter-fill ${conv < 0.7 ? "warn" : ""}`;
    meter.append(fill);
    card.append(meter);

    for (const [k, v] of [
      ["Quantity", sig.quantity != null ? `${sig.quantity} ${(sig.ticker || "").split("/")[0] || ""}` : "--"],
      ["Entry", sig.entry_price != null ? usd(sig.entry_price) : "--"],
      ["Stop-Loss", sig.stop_loss_price != null ? usd(sig.stop_loss_price) : "MISSING"],
      ["Notional", sig.notional != null ? usd(sig.notional) : "--"],
    ]) {
      const row = el("div", "kv");
      row.append(el("span", "kv-key", k), el("span", `kv-val ${k === "Stop-Loss" && sig.stop_loss_price == null ? "down" : ""}`, String(v)));
      card.append(row);
    }
    if (sig.rationale) {
      const r = el("div");
      r.style.cssText = "margin-top:9px;padding-top:9px;border-top:1px solid var(--line);color:var(--muted);font-size:11px";
      r.textContent = sig.rationale;
      card.append(r);
    }
    box.append(card);
  }
}

/* ------------------------------------------------------------ layer 3 -- */
function renderGuardrails(guards) {
  const box = $("guardrails");
  box.textContent = "";
  if (!guards || !guards.length) {
    box.append(el("div", "empty", "evaluating..."));
    $("l3-status").textContent = "--";
    return;
  }

  const failed = guards.filter((g) => !g.passed).length;
  $("l3-status").textContent = failed ? `${failed} BREACH` : "ALL PASS";
  $("l3-status").className = `pill ${failed ? "pill-bad" : "pill-ok"}`;

  for (const g of guards) {
    const row = el("div", "guard");
    const dot = el("div", "guard-dot");
    dot.style.background = g.passed ? "var(--emerald)" : "var(--crimson)";
    dot.style.boxShadow = `0 0 6px ${g.passed ? "#10b98180" : "#ef444480"}`;
    row.append(dot);

    const body = el("div", "guard-body");
    const title = el("div");
    title.style.cssText = "display:flex;justify-content:space-between;align-items:baseline;gap:8px";
    title.append(el("span", "guard-name", g.name));
    title.append(el("span", `pill ${g.passed ? "pill-ok" : "pill-bad"}`, g.passed ? "PASSED" : "FAILED"));
    body.append(title, el("div", "guard-limit", g.limit || ""), el("div", "guard-detail", g.detail || ""));
    row.append(body);
    box.append(row);
  }
}

function renderAudit(entries) {
  const box = $("audit");
  box.textContent = "";
  if (!entries || !entries.length) {
    box.append(el("div", "empty", "no orders processed"));
    $("audit-count").textContent = "0";
    return;
  }
  $("audit-count").textContent = String(entries.length);

  for (const e of entries) {
    const row = el("div", "audit-row");
    row.append(el("span", "audit-time", timeOf(e.timestamp)));
    row.append(el("span", `pill ${e.accepted ? "pill-ok" : "pill-bad"}`, e.accepted ? "ACCEPTED" : "REJECTED"));
    const body = el("div");
    body.append(el("div", "", `${e.ticker || ""} ${e.action || ""} ${e.quantity != null ? e.quantity : ""}`.trim()));
    // The reason is the point of this panel: a rejection that cannot explain
    // itself is an incident waiting to happen.
    body.append(el("div", "audit-reason", e.reason || ""));
    row.append(body);
    box.append(row);
  }
}

function renderBanner(s) {
  const existing = document.querySelector(".banner");
  if (existing) existing.remove();
  if (s.trading_halted) {
    const b = el("div", "banner halt", "KILL SWITCH ACTIVE — TRADING_HALTED is set in config.py. All order flow is rejected.");
    document.querySelector(".grid").prepend(b);
  } else if (s.engine_error) {
    const b = el("div", "banner", `ENGINE DEGRADED — ${s.engine_error}`);
    document.querySelector(".grid").prepend(b);
  }
}

/* --------------------------------------------------------------- poll -- */
async function poll() {
  try {
    const res = await fetch("/api/snapshot", { cache: "no-store" });
    const s = await res.json();
    renderHeader(s);
    renderConnection(true);
    renderKillSwitch(!!s.trading_halted);
    renderNews(s.news);
    renderExtraction(s.latest_extraction);
    renderSignals(s.signals);
    renderGuardrails(s.guardrails);
    renderAudit(s.recent_verdicts);
    renderChart(s.candles);
    renderBanner(s);
  } catch (err) {
    renderConnection(false);
    // Leave the last good values on screen rather than blanking them. A monitoring
    // panel that clears itself when a poll fails destroys the information an
    // operator needs to decide whether the outage or the trading is the problem.
    $("m-conn").textContent = "STALE";
    $("m-conn").className = "pill pill-bad";
  }
}

window.addEventListener("resize", () => poll());
poll();
setInterval(poll, POLL_MS);