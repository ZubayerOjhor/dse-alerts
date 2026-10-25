/* dse-alerts front end: theme switching and charts. No network calls except to this server. */
(function () {
  "use strict";
  var root = document.documentElement;
  var THEMES = ["light", "dark", "midnight", "terminal"];
  var charts = [];

  function cssVar(name) { return getComputedStyle(root).getPropertyValue(name).trim(); }
  function colors() {
    return { bg: cssVar("--surface"), text: cssVar("--ink-2"), grid: cssVar("--line"), border: cssVar("--line-2"),
             up: cssVar("--up"), down: cssVar("--down"), muted: cssVar("--muted"), ink: cssVar("--ink"),
             s1: cssVar("--s1"), s2: cssVar("--s2"), s3: cssVar("--s3"), s4: cssVar("--s4"), font: cssVar("--font") };
  }
  function alpha(hex, a) {
    var h = hex.replace("#", ""); if (h.length === 3) h = h.split("").map(function (c) { return c + c; }).join("");
    var n = parseInt(h, 16); return "rgba(" + (n >> 16 & 255) + "," + (n >> 8 & 255) + "," + (n & 255) + "," + a + ")";
  }
  function fmt(v, d) { if (v === null || v === undefined || isNaN(v)) return "–"; return Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }); }
  /* chart words in the page language (base.html); English if the block is missing */
  var W = (function () {
    var d = { close: "Close", open_s: "O", high_s: "H", low_s: "L", sma20: "SMA 20", sma50: "SMA 50", volume: "Volume", bonus: "{pct}% bonus shares",
              ex_date: "record date", loading: "Loading {range}...", showing: "Showing {range}.", failed: "Could not load {range}. The chart still shows {shown}.",
              price_summary: "{symbol} daily prices, {range}: {n} sessions from {start} to {end}. Last close {close}, highest {high}, lowest {low}.",
              lines_summary: "{series}. From {start} to {end}.", series_from_to: "{name} from {a} to {b}", table_caption: "Last {n} sessions of {range}, newest first",
              sorted: "Sorted by {col}, {dir}.", asc: "ascending", desc: "descending",
              months: ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"] };
    try { var el = document.getElementById("chart-i18n"); if (el) { var o = JSON.parse(el.textContent); for (var k in o) d[k] = o[k]; } } catch (e) { /* keep English */ }
    return d;
  })();
  function fill(tpl, args) { return String(tpl).replace(/\{(\w+)\}/g, function (m, k) { return args[k] !== undefined ? args[k] : m; }); }
  /* "2026-10-19" (or a LightweightCharts business day) -> "19 Oct 2026" in the page language */
  function fdate(t) {
    if (t && typeof t === "object") t = t.year + "-" + String(t.month).padStart(2, "0") + "-" + String(t.day).padStart(2, "0");
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(t || "")); if (!m) return String(t || "");
    return parseInt(m[3], 10) + " " + W.months[parseInt(m[2], 10) - 1] + " " + m[1];
  }
  /* axis labels: thousands separators like the tiles (5,405 not 5404), millions as 7.5M */
  function axis(v) {
    var a = Math.abs(v);
    if (a >= 1e6) return (v / 1e6).toLocaleString("en-US", { maximumFractionDigits: 1 }) + "M";
    return Number(v).toLocaleString("en-US", { maximumFractionDigits: a >= 1000 ? 0 : a >= 100 ? 1 : 2 });
  }
  /* charts size themselves to the container; after a resize or a rotated phone the visible
     range is fitted again so the whole series stays in view */
  function watchResize(container, chart) {
    var timer;
    function fit() { clearTimeout(timer); timer = setTimeout(function () { try { chart.timeScale().fitContent(); } catch (e) { /* removed */ } }, 120); }
    if (window.ResizeObserver) new ResizeObserver(fit).observe(container);
    window.addEventListener("orientationchange", fit);
  }
  function pct(v) { if (v === null || v === undefined || isNaN(v)) return "–"; return (v >= 0 ? "+" : "") + (100 * v).toFixed(1) + "%"; }

  /* ---- theme ---------------------------------------------------------------- */
  function applyTheme(t) {
    root.setAttribute("data-theme", t);
    try { localStorage.setItem("dse-theme", t); } catch (e) { /* private mode */ }
    document.querySelectorAll("[data-theme-pick]").forEach(function (b) { b.classList.toggle("active", b.dataset.themePick === t); });
    charts.forEach(function (c) { c.recolor(); });
  }
  function initTheme() {
    var t = null; try { t = localStorage.getItem("dse-theme"); } catch (e) { /* ignore */ }
    if (!t || THEMES.indexOf(t) < 0) t = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    applyTheme(t);
    document.querySelectorAll("[data-theme-pick]").forEach(function (b) { b.addEventListener("click", function () { applyTheme(b.dataset.themePick); }); });
  }

  /* ---- chart chrome ---------------------------------------------------------- */
  function baseOptions(c) {
    var LW = window.LightweightCharts;
    return {
      layout: { background: { type: "solid", color: c.bg }, textColor: c.text, attributionLogo: false, fontFamily: c.font || undefined, fontSize: 11 },
      grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
      rightPriceScale: { borderColor: c.border, scaleMargins: { top: 0.08, bottom: 0.08 } },
      timeScale: { borderColor: c.border, rightOffset: 3, minBarSpacing: 2, fixLeftEdge: true },
      crosshair: { mode: LW.CrosshairMode.Normal,
                   vertLine: { color: c.muted, width: 1, style: LW.LineStyle.Solid, labelBackgroundColor: c.muted },
                   horzLine: { color: c.muted, width: 1, style: LW.LineStyle.Solid, labelBackgroundColor: c.muted } },
      autoSize: true, handleScale: { axisPressedMouseMove: false },
      localization: { priceFormatter: axis }
    };
  }
  function legendEl(container) {
    var el = container.parentElement.querySelector(".legend");
    if (!el) { el = document.createElement("div"); el.className = "legend"; container.parentElement.insertBefore(el, container); }
    return el;
  }
  function renderLegend(el, items) {
    el.textContent = "";
    items.forEach(function (it) {
      var span = document.createElement("span");
      var key = document.createElement("span"); key.className = "key" + (it.rect ? " rect" : ""); key.style.borderColor = it.color; if (it.rect) key.style.background = it.color;
      span.appendChild(key); span.appendChild(document.createTextNode(it.name));
      var b = document.createElement("b"); b.textContent = it.value; span.appendChild(b);
      el.appendChild(span);
    });
  }

  /* ---- price chart: candles + SMA20/50 + volume pane ------------------------- */
  function priceChart(container, data) {
    var LW = window.LightweightCharts; var c = colors();
    var chart = LW.createChart(container, baseOptions(c));
    var candles = chart.addSeries(LW.CandlestickSeries, { upColor: c.up, downColor: c.down, borderVisible: false, wickUpColor: c.up, wickDownColor: c.down, priceLineVisible: false, priceFormat: { type: "price", precision: 1, minMove: 0.1 } });
    var sma20 = chart.addSeries(LW.LineSeries, { color: c.s1, lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    var sma50 = chart.addSeries(LW.LineSeries, { color: c.s2, lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    var vol = chart.addSeries(LW.HistogramSeries, { priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false }, 1);
    try { chart.panes()[1].setHeight(90); } catch (e) { /* older build */ }
    var legend = legendEl(container);
    var state = { data: data };
    var marks = LW.createSeriesMarkers ? LW.createSeriesMarkers(candles, []) : null;
    function volColor(up) { return alpha(up ? c.up : c.down, 0.6); }
    function markerList(d) {
      return (d.markers || []).map(function (m) {
        return { time: m.time, position: "belowBar", shape: "arrowUp", color: c.s3 || c.muted, size: 1,
                 text: m.bonus ? fill(W.bonus, { pct: m.bonus }) : W.ex_date };
      });
    }
    function load(d) {
      state.data = d;
      candles.setData(d.candles); sma20.setData(d.sma20 || []); sma50.setData(d.sma50 || []);
      if (marks) marks.setMarkers(markerList(d));
      vol.setData((d.volume || []).map(function (v) { return { time: v.time, value: v.value, color: volColor(v.up) }; }));
      chart.timeScale().fitContent();
      showLegend(null);
    }
    function showLegend(param) {
      var d = state.data; if (!d.candles || !d.candles.length) return; var last = d.candles[d.candles.length - 1];
      var cd = param && param.seriesData ? param.seriesData.get(candles) : null;
      var p = cd || last; if (!p) return;
      var s20 = param && param.seriesData ? param.seriesData.get(sma20) : null;
      var s50 = param && param.seriesData ? param.seriesData.get(sma50) : null;
      var vv = param && param.seriesData ? param.seriesData.get(vol) : null;
      var lastV = (d.volume || [])[d.volume.length - 1];
      var items = [
        { name: (param && param.time) ? fdate(param.time) : W.close, color: c.ink, value: fmt(p.close, 1) + "  (" + W.open_s + " " + fmt(p.open, 1) + " " + W.high_s + " " + fmt(p.high, 1) + " " + W.low_s + " " + fmt(p.low, 1) + ")" },
        { name: W.sma20, color: c.s1, value: s20 ? fmt(s20.value, 1) : (d.sma20 && d.sma20.length ? fmt(d.sma20[d.sma20.length - 1].value, 1) : "–") },
        { name: W.sma50, color: c.s2, value: s50 ? fmt(s50.value, 1) : (d.sma50 && d.sma50.length ? fmt(d.sma50[d.sma50.length - 1].value, 1) : "–") },
        { name: W.volume, color: c.muted, rect: true, value: vv ? fmt(vv.value, 0) : (lastV ? fmt(lastV.value, 0) : "–") }
      ];
      renderLegend(legend, items);
    }
    chart.subscribeCrosshairMove(showLegend);
    load(data);
    watchResize(container, chart);
    var h = { chart: chart, load: load, recolor: function () {
      c = colors(); chart.applyOptions(baseOptions(c));
      candles.applyOptions({ upColor: c.up, downColor: c.down, wickUpColor: c.up, wickDownColor: c.down });
      sma20.applyOptions({ color: c.s1 }); sma50.applyOptions({ color: c.s2 });
      if (marks) marks.setMarkers(markerList(state.data));
      vol.setData((state.data.volume || []).map(function (v) { return { time: v.time, value: v.value, color: volColor(v.up) }; }));
      showLegend(null);
    } };
    charts.push(h); return h;
  }

  /* ---- text alternatives: a description on the chart and a table of the latest sessions ---- */
  function describePrice(el, d, rangeName) {
    var cs = d.candles || []; if (!cs.length) return;
    var hi = -Infinity, lo = Infinity;
    cs.forEach(function (k) { if (k.high > hi) hi = k.high; if (k.low < lo) lo = k.low; });
    el.setAttribute("role", "img");
    el.setAttribute("aria-label", fill(W.price_summary, { symbol: el.dataset.symbol || "", range: rangeName, n: cs.length, start: fdate(cs[0].time),
      end: fdate(cs[cs.length - 1].time), close: fmt(cs[cs.length - 1].close, 1), high: fmt(hi, 1), low: fmt(lo, 1) }));
    var table = el.dataset.table && document.getElementById(el.dataset.table); if (!table) return;
    var vols = {}; (d.volume || []).forEach(function (v) { vols[v.time] = v.value; });
    var body = table.tBodies[0]; body.textContent = "";
    var recent = cs.slice(-30).reverse();
    recent.forEach(function (k) {
      var tr = document.createElement("tr");
      [fdate(k.time), fmt(k.open, 1), fmt(k.high, 1), fmt(k.low, 1), fmt(k.close, 1), fmt(vols[k.time], 0)].forEach(function (v, i) {
        var td = document.createElement(i === 0 ? "th" : "td"); if (i === 0) td.scope = "row"; else td.className = "num"; td.textContent = v; tr.appendChild(td);
      });
      body.appendChild(tr);
    });
    if (table.caption) table.caption.textContent = fill(W.table_caption, { n: recent.length, range: rangeName });
  }
  function describeLines(el, handles) {
    var parts = [], start = null, end = null;
    handles.forEach(function (h) {
      if (!h.data.length) return;
      parts.push(fill(W.series_from_to, { name: h.name, a: fmt(h.data[0].value, h.digits), b: fmt(h.data[h.data.length - 1].value, h.digits) }));
      start = start || h.data[0].time; end = h.data[h.data.length - 1].time;
    });
    if (!parts.length) return;
    el.setAttribute("role", "img");
    el.setAttribute("aria-label", fill(W.lines_summary, { series: parts.join("; "), start: fdate(start), end: fdate(end) }));
  }

  /* ---- price range switch: only a response that is current, successful and complete is drawn,
     and the pressed button always names what the chart shows ------------------------------ */
  function rangeSwitch(el, h) {
    var sel = document.querySelector("[data-range-for='" + el.id + "']"); if (!sel) return;
    var buttons = Array.prototype.slice.call(sel.querySelectorAll("button"));
    var status = el.dataset.status && document.getElementById(el.dataset.status);
    var errBox = el.dataset.error && document.getElementById(el.dataset.error);
    var seq = 0, ctrl = null, wanted = null;
    function byRange(r) { return buttons.filter(function (b) { return b.dataset.range === r; })[0]; }
    function nameOf(b) { return b ? (b.dataset.name || b.textContent) : ""; }
    function press(b) { buttons.forEach(function (x) { var on = x === b; x.classList.toggle("active", on); x.setAttribute("aria-pressed", on ? "true" : "false"); }); }
    function say(text) { if (status) status.textContent = text; }
    function valid(nd, rng) {
      if (!nd || nd.range !== rng || !Array.isArray(nd.candles) || !nd.candles.length) return false;
      return nd.candles.every(function (k) { return k && k.time && isFinite(k.open) && isFinite(k.high) && isFinite(k.low) && isFinite(k.close); });
    }
    function fail(b) {
      var shown = byRange(el.dataset.shown);
      press(shown);                                    // the label goes back to what is drawn
      if (errBox) { errBox.hidden = false; errBox.querySelector(".msg").textContent = fill(W.failed, { range: nameOf(b), shown: nameOf(shown) }); }
      say("");
    }
    function load(b) {
      var rng = b.dataset.range, my = ++seq; wanted = b;
      if (ctrl) ctrl.abort();
      ctrl = window.AbortController ? new AbortController() : null;
      if (errBox) errBox.hidden = true;
      press(b); el.classList.add("loading"); el.setAttribute("aria-busy", "true");
      say(fill(W.loading, { range: nameOf(b) }));
      fetch(el.dataset.api + "?range=" + encodeURIComponent(rng), { credentials: "same-origin", signal: ctrl ? ctrl.signal : undefined })
        .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
        .then(function (nd) {
          if (my !== seq) return;                       // a newer click owns the chart
          if (!valid(nd, rng)) throw new Error("bad data");
          h.load(nd); el.dataset.shown = rng; press(b);
          describePrice(el, nd, nameOf(b)); say(fill(W.showing, { range: nameOf(b) }));
        })
        .catch(function (e) { if (my !== seq || (e && e.name === "AbortError")) return; fail(b); })
        .then(function () { if (my === seq) { el.classList.remove("loading"); el.removeAttribute("aria-busy"); } });
    }
    buttons.forEach(function (b) { b.addEventListener("click", function () { load(b); }); });
    if (errBox) { var retry = errBox.querySelector("[data-retry]"); if (retry) retry.addEventListener("click", function () { if (wanted) load(wanted); }); }
  }

  /* ---- line chart: N series on one axis ---------------------------------------- */
  function lineChart(container, series, opts) {
    opts = opts || {};
    var LW = window.LightweightCharts; var c = colors();
    var chart = LW.createChart(container, baseOptions(c));
    var tight = { timeScale: { minBarSpacing: 0.5, rightOffset: 1 } };
    chart.applyOptions(tight);
    var slots = [c.s1, c.s2, c.s3, c.s4];
    var handles = series.map(function (s, i) {
      var color = s.color ? cssVar(s.color) || s.color : slots[i % slots.length];
      var digits = s.digits === undefined ? 1 : s.digits;
      var pf = { type: "price", precision: digits, minMove: Math.pow(10, -digits) };
      var h = chart.addSeries(s.area ? LW.AreaSeries : LW.LineSeries, s.area
        ? { lineColor: color, topColor: alpha(color, 0.25), bottomColor: alpha(color, 0.02), lineWidth: 2, priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: true, priceFormat: pf }
        : { color: color, lineWidth: s.width || 2, lineStyle: s.dashed ? LW.LineStyle.Dashed : LW.LineStyle.Solid, priceLineVisible: false, lastValueVisible: !!s.last, crosshairMarkerVisible: true, priceFormat: pf });
      h.setData(s.data); return { api: h, name: s.name, color: color, data: s.data, digits: s.digits === undefined ? 1 : s.digits };
    });
    chart.timeScale().fitContent();
    var legend = legendEl(container);
    function showLegend(param) {
      renderLegend(legend, handles.map(function (h) {
        var pt = param && param.seriesData ? param.seriesData.get(h.api) : null;
        var v = pt ? pt.value : (h.data.length ? h.data[h.data.length - 1].value : null);
        return { name: h.name, color: h.color, value: opts.format === "pct" ? pct(v) : fmt(v, h.digits) };
      }));
    }
    chart.subscribeCrosshairMove(showLegend); showLegend(null);
    describeLines(container, handles);
    watchResize(container, chart);
    var handle = { chart: chart, recolor: function () { c = colors(); chart.applyOptions(baseOptions(c)); chart.applyOptions(tight); handles.forEach(function (h, i) { if (!series[i].color) { h.color = [c.s1, c.s2, c.s3, c.s4][i % 4]; h.api.applyOptions(series[i].area ? { lineColor: h.color, topColor: alpha(h.color, 0.25), bottomColor: alpha(h.color, 0.02) } : { color: h.color }); } }); showLegend(null); } };
    charts.push(handle); return handle;
  }

  /* ---- boot: every [data-chart] element renders itself from embedded JSON ------- */
  function json(id) { var el = document.getElementById(id); return el ? JSON.parse(el.textContent) : null; }
  function scrollHints() {
    var hint = document.body.dataset.scrollHint;
    document.querySelectorAll(".table-wrap").forEach(function (w) {
      var wide = w.scrollWidth > w.clientWidth + 2;
      w.classList.toggle("scrolls", wide);
      var prev = w.previousElementSibling;
      var h = prev && prev.classList.contains("scroll-hint") ? prev : null;
      if (wide && !h && hint) { h = document.createElement("p"); h.className = "scroll-hint"; h.textContent = hint; w.parentElement.insertBefore(h, w); }
      if (!wide && h) h.remove();
    });
  }
  /* ---- tables sort on a header click; numbers are read out of the cell text ---------------- */
  function cellKey(td) {
    if (td.dataset.sort !== undefined) { var n = parseFloat(td.dataset.sort); return isNaN(n) ? td.dataset.sort : n; }
    var t = (td.textContent || "").trim().replace(/[,৳%x+]/g, "").replace(/\s+/g, " ");
    var m = t.match(/^-?\d+(\.\d+)?/); return m ? parseFloat(m[0]) : t.toLowerCase();
  }
  function sortable() {
    var hint = document.body.dataset.sortHint || "";
    document.querySelectorAll("table.sortable").forEach(function (table) {
      var ths = Array.prototype.slice.call(table.querySelectorAll("thead th"));
      var live = document.createElement("p"); live.className = "sr-only"; live.setAttribute("role", "status"); table.parentElement.insertBefore(live, table);
      ths.forEach(function (th, i) {
        var label = th.textContent.trim(); if (!label) return;
        var b = document.createElement("button"); b.type = "button"; b.className = "th-sort"; if (hint) b.title = hint;
        while (th.firstChild) b.appendChild(th.firstChild);
        th.appendChild(b); th.setAttribute("aria-sort", "none"); th.setAttribute("scope", "col");
        b.addEventListener("click", function () {
          var dir = th.getAttribute("aria-sort") === "ascending" ? "descending" : "ascending";
          ths.forEach(function (x) { if (x.hasAttribute("aria-sort")) x.setAttribute("aria-sort", "none"); });
          th.setAttribute("aria-sort", dir);
          var asc = dir === "ascending";
          var body = table.tBodies[0]; var rows = Array.prototype.slice.call(body.rows);
          rows.sort(function (a, b2) {
            var ka = cellKey(a.cells[i]), kb = cellKey(b2.cells[i]);
            if (typeof ka === "number" && typeof kb === "number") return asc ? ka - kb : kb - ka;
            if (typeof ka === "number") return -1; if (typeof kb === "number") return 1;
            return asc ? String(ka).localeCompare(String(kb)) : String(kb).localeCompare(String(ka));
          });
          rows.forEach(function (r) { body.appendChild(r); });
          live.textContent = fill(W.sorted, { col: label, dir: asc ? W.asc : W.desc });
        });
      });
    });
  }
  /* destructive forms ask first, on every page, toast or not. The answer travels as
     confirmed=1; without JavaScript the server shows its own confirmation page instead. */
  function confirms() {
    document.querySelectorAll("form[data-confirm]").forEach(function (fm) {
      fm.addEventListener("submit", function (e) {
        if (!window.confirm(fm.dataset.confirm)) { e.preventDefault(); return; }
        var c = fm.querySelector("input[name=confirmed]");
        if (!c) { c = document.createElement("input"); c.type = "hidden"; c.name = "confirmed"; fm.appendChild(c); }
        c.value = "1";
      });
    });
  }
  function flashes() {
    var f = document.getElementById("flash"); if (!f) return;
    var x = f.querySelector("[data-dismiss]"); if (x) x.addEventListener("click", function () { f.classList.add("gone"); });
    if (!f.querySelector("form")) setTimeout(function () { f.classList.add("gone"); }, 9000);
  }
  function boot() {
    initTheme();
    confirms(); scrollHints(); sortable(); flashes();
    var rt; window.addEventListener("resize", function () { clearTimeout(rt); rt = setTimeout(scrollHints, 150); });
    if (!window.LightweightCharts) return;
    document.querySelectorAll("[data-chart]").forEach(function (el) {
      var kind = el.dataset.chart; var d = json(el.dataset.src);
      if (!d) return;
      if (kind === "price") {
        var h = priceChart(el, d);
        var first = document.querySelector("[data-range-for='" + el.id + "'] button[data-range='" + (el.dataset.shown || "1y") + "']");
        describePrice(el, d, first ? (first.dataset.name || first.textContent) : "");
        rangeSwitch(el, h);
      } else if (kind === "lines") {
        lineChart(el, d.series, d.options || {});
      }
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
  window.dse = { applyTheme: applyTheme, priceChart: priceChart, lineChart: lineChart };
})();
