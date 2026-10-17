// Readable earnings-mention strikes for one venue. The page sets data-src on <body>
// ("kalshi" | "polymarket"); data comes from data/<src>_mentions.json (written by the sheet scripts).
// Kalshi strikes carry each side's order book, so that page adds a pre-trade check: all-in cost
// with Kalshi's fee, payout per $1, depth within 3¢ of the ask and the average fill for an order size.
(() => {
  const src = document.body.dataset.src;
  const kalshi = src === "kalshi";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  const day = (iso) => new Date(iso + "T12:00:00").toLocaleDateString("en-US", {weekday: "short", month: "short", day: "numeric"});
  const c = (v) => v == null ? "–" : (Math.round(v * 10) / 10) + "¢";
  const money = (v) => v == null ? "–" : (src === "polymarket" ? "$" : "") +
    (v >= 1000 ? (v / 1000).toFixed(v >= 10000 ? 0 : 1) + "K" : Math.round(v).toLocaleString());
  const n = (v) => v == null ? "–" : Math.round(v).toLocaleString();
  let data = null;

  // ---- Kalshi pre-trade math (mirrors mention_sheet.py)
  const fee = (p) => 0.07 * (p / 100) * (1 - p / 100) * 100;  // taker fee per contract, ¢
  function fill(book, budget) {
    // walk the asks cheapest first, spending `budget` dollars with fees included
    let left = budget * 100, got = 0, spent = 0;
    for (const [p, q] of book || []) {
      const cost = p + fee(p), take = Math.min(q, left / cost);
      got += take; spent += take * p; left -= take * cost;
      if (left <= 1e-9) break;
    }
    if (!got) return null;
    return {avg: spent / got, allIn: (budget * 100 - left) / got, n: got, full: left <= 1e-6};
  }
  const depthCls = (d) => d == null ? "" : d < 100 ? "lo" : d <= 300 ? "mid" : "hi";

  function hl(text, q) {
    const t = esc(text);
    if (!q) return t;
    const i = t.toLowerCase().indexOf(esc(q).toLowerCase());
    return i < 0 ? t : t.slice(0, i) + "<mark>" + t.slice(i, i + q.length) + "</mark>" + t.slice(i + q.length);
  }

  const pct = (v) => v == null ? "–" : Math.round(v) + "%";
  const payoutAt = (cost) => cost ? (100 / cost - 1) * 100 : null;

  // Tradeable side: depth isn't red (100+ within 3¢), the order fills in full, and the payout
  // at that fill is 33% or more. A strike is tradeable when either side is.
  function check(sd, budget) {
    if (!sd || sd.ask == null) return {ok: false};
    const f = fill(sd.book, budget);
    const pay = f && f.full ? payoutAt(f.allIn) : null;
    return {ok: sd.depth >= 100 && pay != null && pay >= 33, f, pay};
  }
  const tradeable = (s, budget) => check(s.yes, budget).ok || check(s.no, budget).ok;

  function shortlist(events, q, budget) {
    const usd = "$" + budget.toLocaleString();
    const rows = [];
    for (const {ev, strikes} of events) {
      for (const s of strikes) {
        for (const [name, k] of [["Yes", "yes"], ["No", "no"]]) {
          const ck = check(s[k], budget);
          if (ck.ok) rows.push({ev, s, name, sd: s[k], ck});
        }
      }
    }
    rows.sort((a, b) => a.ev.date.localeCompare(b.ev.date) || a.ev.company.localeCompare(b.ev.company) || b.ck.pay - a.ck.pay);
    const body = rows.map(({ev, s, name, sd, ck}) => `<tr>
        <td class="d">${day(ev.date)}</td>
        <td><a href="${esc(ev.url)}" target="_blank" rel="noopener">${esc(ev.company)}</a></td>
        <td class="w">${hl(s.word, q)}</td>
        <td><span class="sidetag ${name.toLowerCase()}">Buy ${name}</span></td>
        <td><b>${c(sd.ask)}</b><small class="dim">${c(sd.all_in)} with fee</small></td>
        <td class="dp ${depthCls(sd.depth)}">${n(sd.depth)}</td>
        <td><b>${c(ck.f.allIn)}</b><small class="dim">${n(ck.f.n)} contracts</small></td>
        <td><b>${pct(ck.pay)}</b></td>
        <td class="dim">${pct(sd.payout)}</td></tr>`).join("");
    return `<section class="short">
      <h2>Tradeable <span class="d">${rows.length} side${rows.length === 1 ? "" : "s"} at ${usd}</span></h2>
      <p class="emeta">Depth isn't red (100+ contracts within 3¢), a ${usd} order fills in full, and the payout at that fill is 33% or more. Sorted by call date.</p>
      ${rows.length ? `<div class="tbl"><table class="st">
        <thead><tr><th>Call</th><th>Company</th><th>Word</th><th>Side</th><th>Ask</th><th>≤3¢ depth</th><th>${usd} fill<br>(with fees)</th><th>Payout at fill</th><th>Payout at ask</th></tr></thead>
        <tbody>${body}</tbody></table></div>` : `<p class="empty">Nothing passes at ${usd}. Try a smaller order size.</p>`}
    </section><h2 class="all">All strikes</h2>`;
  }

  function sideCells(sd, budget) {
    if (!sd || sd.ask == null) return `<td class="dim">–</td><td></td><td></td><td></td><td class="sep"></td>`;
    const f = fill(sd.book, budget);
    let fillCell, fillPay = `<td class="sep dim">–</td>`;
    if (!f) {
      fillCell = `<td class="part">No offers in book</td>`;
    } else if (!f.full) {
      // the whole visible book can't take the order: no honest average, only a limit order works
      fillCell = `<td class="part" title="Average ${c(f.allIn)} with fees for what is there">Partial fill: ${n(f.n)} contracts available</td>`;
    } else {
      const slip = f.allIn - sd.all_in;
      const p = payoutAt(f.allIn);
      fillCell = `<td title="${n(f.n)} contracts"><b>${c(f.allIn)}</b>` +
        `<small class="dim">${c(f.avg)} before fees</small>` +
        `<small class="${slip >= 3 ? "bad" : "dim"}">${slip > 0.05 ? "+" + c(slip) + " slippage" : "no slippage"}</small></td>`;
      fillPay = `<td class="sep ${p < 33 ? "bad" : ""}"><b>${pct(p)}</b></td>`;
    }
    return `<td><b>${c(sd.ask)}</b><small class="dim">${c(sd.all_in)} with fee</small></td>` +
      `<td class="${sd.payout != null && sd.payout < 33 ? "bad" : ""}">${pct(sd.payout)}</td>` +
      `<td class="dp ${depthCls(sd.depth)}">${n(sd.depth)}</td>` + fillCell + fillPay;
  }

  function kalshiTable(rows, q, budget) {
    const usd = "$" + budget.toLocaleString();
    const head = (s) => `<th>${s} ask</th><th title="Profit per $1 if it wins, at the ask plus fee">Payout at ask</th>` +
      `<th title="Contracts you can buy within 3¢ of the ask">≤3¢ depth</th>` +
      `<th title="Average price walking the order book, fees included">${usd} fill<br>(with fees)</th>` +
      `<th class="sep" title="Profit per $1 at the ${usd} fill price, fees included">Payout at fill</th>`;
    return `<table class="kt">
      <thead><tr><th rowspan="2">Word</th><th colspan="5" class="grp sep">Buy Yes</th><th colspan="5" class="grp sep">Buy No</th><th rowspan="2">Volume</th></tr>
      <tr>${head("Yes")}${head("No")}</tr></thead>
      <tbody>${rows.map((s) => `<tr${tradeable(s, budget) ? "" : ` class="dead"`}><td class="w">${hl(s.word, q)}</td>${sideCells(s.yes, budget)}${sideCells(s.no, budget)}<td>${n(s.volume)}</td></tr>`).join("")}</tbody>
    </table>`;
  }

  function plainTable(rows, q) {
    return `<table>
      <thead><tr><th>Word</th><th>Yes</th><th class="hide-sm">Yes bid</th><th>No</th><th class="hide-sm">Last</th><th class="hide-sm">Spread</th><th>Volume</th></tr></thead>
      <tbody>${rows.map((s) => {
        const p = s.yes_ask ?? s.last;
        return `<tr>
          <td class="w">${hl(s.word, q)}</td>
          <td class="p"><b>${c(s.yes_ask)}</b><span class="meter" aria-hidden="true"><i style="width:${Math.max(0, Math.min(100, p ?? 0))}%"></i></span></td>
          <td class="hide-sm dim">${c(s.yes_bid)}</td>
          <td>${c(s.no_ask)}</td>
          <td class="hide-sm dim">${c(s.last)}</td>
          <td class="hide-sm dim">${c(s.spread)}</td>
          <td>${money(s.volume)}</td></tr>`;
      }).join("")}</tbody>
    </table>`;
  }

  function render() {
    const q = $("q").value.trim().toLowerCase();
    const when = $("when").value;
    const sort = $("sort").value;
    const budget = Math.max(1, Number($("order")?.value) || 100);
    const yesAsk = (s) => (kalshi ? s.yes?.ask : s.yes_ask) ?? s.last ?? -1;
    const by = {
      word: (a, b) => a.word.localeCompare(b.word),
      volume: (a, b) => (b.volume || 0) - (a.volume || 0),
      depth: (a, b) => (b.yes?.depth || 0) - (a.yes?.depth || 0),
      payout: (a, b) => (b.yes?.payout ?? -1) - (a.yes?.payout ?? -1),
      price: (a, b) => yesAsk(b) - yesAsk(a),
    }[sort] || ((a, b) => yesAsk(b) - yesAsk(a));
    let shown = 0, calls = 0;
    const html = [], picked = [];
    for (const ev of data.events) {
      if (when && ev.date !== when) continue;
      const companyHit = ev.company.toLowerCase().includes(q);
      const rows = ev.strikes.filter((s) => !q || companyHit || s.word.toLowerCase().includes(q));
      if (!rows.length) continue;
      if (kalshi) {
        // tradeable strikes first, dead ones (red depth, partial fill or thin payout) at the bottom
        const ok = new Map(rows.map((s) => [s, tradeable(s, budget)]));
        rows.sort((a, b) => (ok.get(b) - ok.get(a)) || by(a, b));
        picked.push({ev, strikes: rows});
      } else {
        rows.sort(by);
      }
      shown += rows.length; calls++;
      const wq = companyHit ? "" : q;
      html.push(`<section>
        <h2><span class="d">${day(ev.date)}</span><a href="${esc(ev.url)}" target="_blank" rel="noopener">${hl(ev.company, companyHit ? q : "")}</a></h2>
        <p class="emeta">${ev.strikes.length} words · ${money(ev.volume)} volume${src === "polymarket" ? " · resolves by this date" : ""}</p>
        <div class="tbl">${kalshi ? kalshiTable(rows, wq, budget) : plainTable(rows, wq)}</div></section>`);
    }
    $("list").innerHTML = html.length
      ? (kalshi ? shortlist(picked, q, budget) : "") + html.join("")
      : `<p class="empty">No words match.</p>`;
    $("summary").textContent = `${shown} words across ${calls} earnings call${calls === 1 ? "" : "s"}` +
      (kalshi ? ` · Fees (0.07 × P × (1−P)) are included in every figure except "before fees". Payout = profit per $1, red under 33%; payout at fill is what a $${budget.toLocaleString()} order would actually earn. Depth: red under 100, yellow 100–300, green over 300. Partial fill = the whole book can't take the order, so only a limit order works.`
              : " · Yes and No are ask prices (what it costs to buy)");
  }

  fetch(`data/${src}_mentions.json?t=${Date.now()}`)
    .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then((d) => {
      data = d;
      data.events.sort((a, b) => a.date.localeCompare(b.date) || a.company.localeCompare(b.company));
      const total = data.events.reduce((t, e) => t + e.strikes.length, 0);
      const gen = new Date(data.generated).toLocaleString("en-US", {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"});
      $("updated").textContent = `${total} words across ${data.events.length} call${data.events.length === 1 ? "" : "s"}, ${day(data.from)} – ${day(data.to)} · updated ${gen}`;
      const dates = [...new Set(data.events.map((e) => e.date))];
      $("when").innerHTML = `<option value="">All dates</option>` + dates.map((d) => `<option value="${d}">${day(d)}</option>`).join("");
      if (kalshi) $("sort").insertAdjacentHTML("beforeend",
        `<option value="payout">Best Yes payout</option><option value="depth">Most Yes depth</option>`);
      for (const id of ["q", "when", "sort", "order"]) $(id)?.addEventListener("input", render);
      const sizes = document.querySelectorAll(".size");
      const markSize = () => sizes.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === $("order").value)));
      sizes.forEach((b) => b.addEventListener("click", () => { $("order").value = b.dataset.v; markSize(); render(); }));
      $("order")?.addEventListener("input", markSize);
      if (sizes.length) markSize();
      render();
    })
    .catch(() => {
      $("updated").textContent = "Couldn't load the mention data. It's rebuilt with each dashboard refresh.";
      $("list").innerHTML = "";
    });
})();
