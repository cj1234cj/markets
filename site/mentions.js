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

  function sideCells(sd, budget) {
    if (!sd || sd.ask == null) return `<td class="dim">–</td><td></td><td></td><td></td><td class="sep"></td>`;
    const f = fill(sd.book, budget);
    const slip = f ? f.avg - sd.ask : null;
    const fillCell = !f ? `<td class="dim sep">no book</td>`
      : `<td class="sep" title="All-in ${c(f.allIn)} with fees · ${n(f.n)} contracts">${c(f.avg)}` +
        `<small class="${slip >= 3 ? "bad" : "dim"}">${slip > 0.05 ? "+" + c(slip) : "at ask"}</small>` +
        (f.full ? "" : `<small class="bad">only ${n(f.n)}</small>`) + `</td>`;
    return `<td><b>${c(sd.ask)}</b><small class="dim">${c(sd.all_in)} all-in</small></td>` +
      `<td class="${sd.payout != null && sd.payout < 33 ? "bad" : ""}">${sd.payout == null ? "–" : Math.round(sd.payout) + "%"}</td>` +
      `<td class="dp ${depthCls(sd.depth)}">${n(sd.depth)}</td>` + fillCell;
  }

  function kalshiTable(rows, q, budget) {
    const usd = "$" + budget.toLocaleString();
    const head = (s) => `<th>${s} ask</th><th title="Profit per $1 if it wins, at the all-in cost">Payout</th>` +
      `<th title="Contracts you can buy within 3¢ of the ask">≤3¢ depth</th><th class="sep" title="Average fill walking the order book, before fees">${usd} fill</th>`;
    return `<table class="kt">
      <thead><tr><th rowspan="2">Word</th><th colspan="4" class="grp sep">Buy Yes</th><th colspan="4" class="grp sep">Buy No</th><th rowspan="2">Volume</th></tr>
      <tr>${head("Yes")}${head("No")}</tr></thead>
      <tbody>${rows.map((s) => `<tr><td class="w">${hl(s.word, q)}</td>${sideCells(s.yes, budget)}${sideCells(s.no, budget)}<td>${n(s.volume)}</td></tr>`).join("")}</tbody>
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
    const html = [];
    for (const ev of data.events) {
      if (when && ev.date !== when) continue;
      const companyHit = ev.company.toLowerCase().includes(q);
      const rows = ev.strikes.filter((s) => !q || companyHit || s.word.toLowerCase().includes(q));
      if (!rows.length) continue;
      rows.sort(by);
      shown += rows.length; calls++;
      const wq = companyHit ? "" : q;
      html.push(`<section>
        <h2><span class="d">${day(ev.date)}</span><a href="${esc(ev.url)}" target="_blank" rel="noopener">${hl(ev.company, companyHit ? q : "")}</a></h2>
        <p class="emeta">${ev.strikes.length} words · ${money(ev.volume)} volume${src === "polymarket" ? " · resolves by this date" : ""}</p>
        <div class="tbl">${kalshi ? kalshiTable(rows, wq, budget) : plainTable(rows, wq)}</div></section>`);
    }
    $("list").innerHTML = html.join("") || `<p class="empty">No words match.</p>`;
    $("summary").textContent = `${shown} words across ${calls} earnings call${calls === 1 ? "" : "s"}` +
      (kalshi ? " · Ask is the best price; all-in adds Kalshi's fee (0.07 × P × (1−P)). Payout is profit per $1 at the all-in cost, red under 33%. Depth: red under 100, yellow 100–300, green over 300. Fill walks the order book; +¢ is slippage over the ask."
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
      render();
    })
    .catch(() => {
      $("updated").textContent = "Couldn't load the mention data. It's rebuilt with each dashboard refresh.";
      $("list").innerHTML = "";
    });
})();
