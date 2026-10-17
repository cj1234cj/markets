// Readable earnings-mention strikes for one venue. The page sets data-src on <body>
// ("kalshi" | "polymarket"); data comes from data/<src>_mentions.json (written by the sheet scripts).
(() => {
  const src = document.body.dataset.src;
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  const day = (iso) => new Date(iso + "T12:00:00").toLocaleDateString("en-US", {weekday: "short", month: "short", day: "numeric"});
  const c = (v) => v == null ? "–" : (Math.round(v * 10) / 10) + "¢";
  const money = (v) => v == null ? "–" : (src === "polymarket" ? "$" : "") +
    (v >= 1000 ? (v / 1000).toFixed(v >= 10000 ? 0 : 1) + "K" : Math.round(v).toLocaleString());
  const n = (v) => v == null ? "–" : Math.round(v).toLocaleString();
  const hasDepth = src === "kalshi";  // Kalshi orderbook depth: contracts within 3¢ of the ask
  let data = null;

  function hl(text, q) {
    const t = esc(text);
    if (!q) return t;
    const i = t.toLowerCase().indexOf(esc(q).toLowerCase());
    return i < 0 ? t : t.slice(0, i) + "<mark>" + t.slice(i, i + q.length) + "</mark>" + t.slice(i + q.length);
  }

  function render() {
    const q = $("q").value.trim().toLowerCase();
    const when = $("when").value;
    const sort = $("sort").value;
    let shown = 0, calls = 0;
    const html = [];
    for (const ev of data.events) {
      if (when && ev.date !== when) continue;
      const companyHit = ev.company.toLowerCase().includes(q);
      const rows = ev.strikes.filter((s) => !q || companyHit || s.word.toLowerCase().includes(q));
      if (!rows.length) continue;
      const yes = (s) => s.yes_ask ?? s.last ?? -1;
      rows.sort(sort === "word" ? (a, b) => a.word.localeCompare(b.word)
        : sort === "volume" ? (a, b) => (b.volume || 0) - (a.volume || 0)
        : sort === "depth" ? (a, b) => (b.yes_depth || 0) - (a.yes_depth || 0)
        : (a, b) => yes(b) - yes(a));
      shown += rows.length; calls++;
      html.push(`<section>
        <h2><span class="d">${day(ev.date)}</span><a href="${esc(ev.url)}" target="_blank" rel="noopener">${hl(ev.company, companyHit ? q : "")}</a></h2>
        <p class="emeta">${ev.strikes.length} words · ${money(ev.volume)} volume${src === "polymarket" ? " · resolves by this date" : ""}</p>
        <div class="tbl"><table>
          <thead><tr><th>Word</th><th>Yes</th>${hasDepth ? '<th title="Yes contracts you can buy within 3¢ of the ask">Yes depth</th>' : ""}<th class="hide-sm">Yes bid</th><th>No</th>${hasDepth ? '<th title="No contracts you can buy within 3¢ of the ask">No depth</th>' : ""}<th class="hide-sm">Last</th><th class="hide-sm">Spread</th><th>Volume</th></tr></thead>
          <tbody>${rows.map((s) => {
            const p = s.yes_ask ?? s.last;
            return `<tr>
              <td class="w">${hl(s.word, companyHit ? "" : q)}</td>
              <td class="p"><b>${c(s.yes_ask)}</b><span class="meter" aria-hidden="true"><i style="width:${Math.max(0, Math.min(100, p ?? 0))}%"></i></span></td>
              ${hasDepth ? `<td>${n(s.yes_depth)}</td>` : ""}
              <td class="hide-sm dim">${c(s.yes_bid)}</td>
              <td>${c(s.no_ask)}</td>
              ${hasDepth ? `<td>${n(s.no_depth)}</td>` : ""}
              <td class="hide-sm dim">${c(s.last)}</td>
              <td class="hide-sm dim">${c(s.spread)}</td>
              <td>${money(s.volume)}</td></tr>`;
          }).join("")}</tbody>
        </table></div></section>`);
    }
    $("list").innerHTML = html.join("") || `<p class="empty">No words match.</p>`;
    $("summary").textContent = `${shown} words across ${calls} earnings call${calls === 1 ? "" : "s"}` +
      " · Yes and No are ask prices (what it costs to buy)" +
      (hasDepth ? " · Depth = contracts you can buy within 3¢ of that ask" : "");
  }

  fetch(`data/${src}_mentions.json?t=${Date.now()}`)
    .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then((d) => {
      data = d;
      data.events.sort((a, b) => a.date.localeCompare(b.date) || a.company.localeCompare(b.company));
      const n = data.events.reduce((t, e) => t + e.strikes.length, 0);
      const gen = new Date(data.generated).toLocaleString("en-US", {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"});
      $("updated").textContent = `${n} words across ${data.events.length} call${data.events.length === 1 ? "" : "s"}, ${day(data.from)} – ${day(data.to)} · updated ${gen}`;
      const dates = [...new Set(data.events.map((e) => e.date))];
      $("when").innerHTML = `<option value="">All dates</option>` + dates.map((d) => `<option value="${d}">${day(d)}</option>`).join("");
      if (hasDepth) $("sort").insertAdjacentHTML("beforeend", `<option value="depth">Most Yes depth</option>`);
      for (const id of ["q", "when", "sort"]) $(id).addEventListener("input", render);
      render();
    })
    .catch(() => {
      $("updated").textContent = "Couldn't load the mention data. It's rebuilt with each dashboard refresh.";
      $("list").innerHTML = "";
    });
})();
