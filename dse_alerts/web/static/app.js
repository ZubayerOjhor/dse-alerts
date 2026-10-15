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
  function fmt(v, d) { if (v === null || v === undefined || isNaN(v)) return "–"; return Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }); }
  /* axis labels: thousands separators like the tiles (5,405 not 5404), millions as 7.5M */
  function axis(v) {
    var a = Math.abs(v);
    if (a >= 1e6) return (v / 1e6).toLocaleString(undefined, { maximumFractionDigits: 1 }) + "M";
    return Number(v).toLocaleString(undefined, { maximumFractionDigits: a >= 1000 ? 0 : a >= 100 ? 1 : 2 });
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
    var words = (document.body.dataset.chartWords || "{pct}% bonus shares|record date").split("|");
    var marks = LW.createSeriesMarkers ? LW.createSeriesMarkers(candles, []) : null;
    function volColor(up) { return alpha(up ? c.up : c.down, 0.6); }
    function markerList(d) {
      return (d.markers || []).map(function (m) {
        return { time: m.time, position: "belowBar", shape: "arrowUp", color: c.s3 || c.muted, size: 1,
                 text: m.bonus ? words[0].replace("{pct}", m.bonus) : words[1] };
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
      var d = state.data; var last = d.candles[d.candles.length - 1];
      var cd = param && param.seriesData ? param.seriesData.get(candles) : null;
      var p = cd || last; if (!p) return;
      var s20 = param && param.seriesData ? param.seriesData.get(sma20) : null;
      var s50 = param && param.seriesData ? param.seriesData.get(sma50) : null;
      var vv = param && param.seriesData ? param.seriesData.get(vol) : null;
      var lastV = (d.volume || [])[d.volume.length - 1];
      var items = [
        { name: (param && param.time) ? String(param.time) : "Close", color: c.ink, value: fmt(p.close, 1) + "  (O " + fmt(p.open, 1) + " H " + fmt(p.high, 1) + " L " + fmt(p.low, 1) + ")" },
        { name: "SMA 20", color: c.s1, value: s20 ? fmt(s20.value, 1) : (d.sma20 && d.sma20.length ? fmt(d.sma20[d.sma20.length - 1].value, 1) : "–") },
        { name: "SMA 50", color: c.s2, value: s50 ? fmt(s50.value, 1) : (d.sma50 && d.sma50.length ? fmt(d.sma50[d.sma50.length - 1].value, 1) : "–") },
        { name: "Volume", color: c.muted, rect: true, value: vv ? fmt(vv.value, 0) : (lastV ? fmt(lastV.value, 0) : "–") }
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
    document.querySelectorAll("table.sortable").forEach(function (table) {
      var ths = table.querySelectorAll("thead th");
      ths.forEach(function (th, i) {
        if (!th.textContent.trim()) return;
        th.addEventListener("click", function () {
          var dir = th.classList.contains("asc") ? "desc" : "asc";
          ths.forEach(function (x) { x.classList.remove("asc", "desc"); }); th.classList.add(dir);
          var body = table.tBodies[0]; var rows = Array.prototype.slice.call(body.rows);
          rows.sort(function (a, b) {
            var ka = cellKey(a.cells[i]), kb = cellKey(b.cells[i]);
            if (typeof ka === "number" && typeof kb === "number") return dir === "asc" ? ka - kb : kb - ka;
            if (typeof ka === "number") return -1; if (typeof kb === "number") return 1;
            return dir === "asc" ? String(ka).localeCompare(String(kb)) : String(kb).localeCompare(String(ka));
          });
          rows.forEach(function (r) { body.appendChild(r); });
        });
      });
    });
  }
  function flashes() {
    var f = document.getElementById("flash"); if (!f) return;
    var x = f.querySelector("[data-dismiss]"); if (x) x.addEventListener("click", function () { f.classList.add("gone"); });
    if (!f.querySelector("form")) setTimeout(function () { f.classList.add("gone"); }, 9000);
    document.querySelectorAll("form[data-confirm]").forEach(function (fm) {
      fm.addEventListener("submit", function (e) { if (!window.confirm(fm.dataset.confirm)) e.preventDefault(); });
    });
  }
  function boot() {
    initTheme();
    scrollHints(); sortable(); flashes();
    var rt; window.addEventListener("resize", function () { clearTimeout(rt); rt = setTimeout(scrollHints, 150); });
    if (!window.LightweightCharts) return;
    document.querySelectorAll("[data-chart]").forEach(function (el) {
      var kind = el.dataset.chart; var d = json(el.dataset.src);
      if (!d) return;
      if (kind === "price") {
        var h = priceChart(el, d);
        var sel = document.querySelector("[data-range-for='" + el.id + "']");
        if (sel) sel.querySelectorAll("button").forEach(function (b) {
          b.addEventListener("click", function () {
            sel.querySelectorAll("button").forEach(function (x) { x.classList.toggle("active", x === b); });
            el.classList.add("loading");
            fetch(el.dataset.api + "?range=" + encodeURIComponent(b.dataset.range), { credentials: "same-origin" })
              .then(function (r) { return r.json(); }).then(function (nd) { h.load(nd); }).catch(function () { /* keep frame */ })
              .finally(function () { el.classList.remove("loading"); });
          });
        });
      } else if (kind === "lines") {
        lineChart(el, d.series, d.options || {});
      }
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
  window.dse = { applyTheme: applyTheme, priceChart: priceChart, lineChart: lineChart };
})();
