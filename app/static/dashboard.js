/*
 * Painel do Nuna Analytics. JS puro, sem dependências nem CDN (a CSP do
 * servidor proíbe qualquer origem externa).
 *
 * Todo texto vindo da API entra com textContent, nunca innerHTML: os valores
 * já são enums e slugs validados, mas o painel não deve depender disso.
 */
(function () {
  "use strict";

  var TOKEN_KEY = "nuna.analytics.adminToken";
  var SVG_NS = "http://www.w3.org/2000/svg";
  var CONTEXT_FIELDS = ["app_version", "build", "os_version", "device_family", "layout", "subscription_state"];
  var DEFAULT_FUNNEL = "paywall_viewed,subscribe_tapped,parental_gate_shown:purpose=subscribe,parental_gate_passed:purpose=subscribe,purchase_completed";

  var memoryToken = null; // se sessionStorage estiver bloqueado
  var catalog = null;
  var numberFormat = new Intl.NumberFormat("pt-BR");
  var percentFormat = new Intl.NumberFormat("pt-BR", { style: "percent", maximumFractionDigits: 1 });
  var monthNames = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"];

  function $(id) { return document.getElementById(id); }

  // DOM ---------------------------------------------------------------------

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        if (key === "text") node.textContent = attrs[key];
        else if (key === "className") node.className = attrs[key];
        else node.setAttribute(key, attrs[key]);
      });
    }
    (children || []).forEach(function (child) {
      if (child == null) return;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  function svg(tag, attrs) {
    var node = document.createElementNS(SVG_NS, tag);
    Object.keys(attrs || {}).forEach(function (key) { node.setAttribute(key, attrs[key]); });
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
    return node;
  }

  function fmt(n) { return n == null ? "—" : numberFormat.format(n); }
  function pct(x) { return x == null ? "—" : percentFormat.format(x); }
  function valueText(v) { return v === null || v === undefined ? "(ausente)" : String(v); }

  function shortDay(iso) {
    var parts = iso.split("-");
    return parseInt(parts[2], 10) + " " + monthNames[parseInt(parts[1], 10) - 1];
  }

  // Token ---------------------------------------------------------------------

  function getToken() {
    try { return window.sessionStorage.getItem(TOKEN_KEY) || memoryToken; } catch (e) { return memoryToken; }
  }

  function setToken(token) {
    memoryToken = token;
    try {
      if (token) window.sessionStorage.setItem(TOKEN_KEY, token);
      else window.sessionStorage.removeItem(TOKEN_KEY);
    } catch (e) { /* segue só em memória */ }
  }

  function showLogin(message) {
    setToken(null);
    $("app").hidden = true;
    $("logout").hidden = true;
    $("login").hidden = false;
    var error = $("login-error");
    error.textContent = message || "";
    error.hidden = !message;
    $("token").value = "";
    $("token").focus();
  }

  function showApp() {
    $("login").hidden = true;
    $("app").hidden = false;
    $("logout").hidden = false;
  }

  // API -----------------------------------------------------------------------

  function AuthError() { this.name = "AuthError"; }

  function api(path, params) {
    var url = new URL(path, window.location.origin);
    Object.keys(params || {}).forEach(function (key) {
      if (params[key] !== undefined && params[key] !== null && params[key] !== "") {
        url.searchParams.set(key, params[key]);
      }
    });
    var headers = {};
    var token = getToken();
    if (token) headers.Authorization = "Bearer " + token;
    return fetch(url.toString(), { headers: headers, cache: "no-store", credentials: "omit" })
      .then(function (res) {
        if (res.status === 401 && path.indexOf("/v1/stats") === 0) throw new AuthError();
        return res.json().catch(function () { return {}; }).then(function (body) {
          if (!res.ok) {
            var detail = typeof body.detail === "string" ? body.detail : "HTTP " + res.status;
            throw new Error(detail);
          }
          return body;
        });
      });
  }

  function range() {
    return { from: $("from").value, to: $("to").value };
  }

  // Tooltip -------------------------------------------------------------------

  var tooltip = null;

  function showTip(text, event) {
    tooltip.textContent = text;
    tooltip.hidden = false;
    var x = event.clientX;
    var y = event.clientY;
    if (x === undefined || (x === 0 && y === 0)) {
      var box = event.target.getBoundingClientRect();
      x = box.left + box.width / 2;
      y = box.top;
    }
    var w = tooltip.offsetWidth;
    var left = Math.min(Math.max(8, x - w / 2), window.innerWidth - w - 8);
    tooltip.style.left = left + "px";
    tooltip.style.top = Math.max(8, y - tooltip.offsetHeight - 12) + "px";
  }

  function hideTip() { tooltip.hidden = true; }

  // Gráficos --------------------------------------------------------------------

  function niceMax(value) {
    if (value <= 0) return 1;
    var magnitude = Math.pow(10, Math.floor(Math.log10(value)));
    var steps = [1, 2, 2.5, 5, 10];
    for (var i = 0; i < steps.length; i++) {
      if (steps[i] * magnitude >= value) return steps[i] * magnitude;
    }
    return 10 * magnitude;
  }

  // Coluna com topo arredondado (4px) e base reta, presa na linha de base.
  function barPath(x, y, w, h, r) {
    r = Math.min(r, w / 2, h);
    return [
      "M", x, y + h,
      "L", x, y + r,
      "Q", x, y, x + r, y,
      "L", x + w - r, y,
      "Q", x + w, y, x + w, y + r,
      "L", x + w, y + h,
      "Z"
    ].join(" ");
  }

  /** points: [{label, value}] em ordem de dia; unit: texto do tooltip. */
  function columnChart(container, points, unit) {
    clear(container);
    if (!points.length) {
      container.appendChild(el("p", { className: "empty", text: "Sem dados no período." }));
      return;
    }
    var width = Math.max(280, Math.round(container.clientWidth || 480));
    var height = 180;
    var m = { top: 10, right: 4, bottom: 24, left: 40 };
    var plotW = width - m.left - m.right;
    var plotH = height - m.top - m.bottom;
    var max = niceMax(Math.max.apply(null, points.map(function (p) { return p.value; })));
    var band = plotW / points.length;
    var gap = band > 4 ? 2 : 0;
    var barW = Math.max(1, band - gap);
    var total = points.reduce(function (sum, p) { return sum + p.value; }, 0);

    var root = svg("svg", {
      viewBox: "0 0 " + width + " " + height,
      role: "img",
      "aria-label": unit + ": " + fmt(total) + " no período, máximo diário " + fmt(Math.max.apply(null, points.map(function (p) { return p.value; })))
    });

    [0, 0.5, 1].forEach(function (f) {
      var y = m.top + plotH - f * plotH;
      if (f > 0) root.appendChild(svg("line", { x1: m.left, x2: width - m.right, y1: y, y2: y, "class": "gridline" }));
      var label = svg("text", { x: m.left - 8, y: y + 4, "text-anchor": "end", "class": "axis-label" });
      label.textContent = fmt(max * f);
      root.appendChild(label);
    });

    points.forEach(function (p, i) {
      var x = m.left + i * band + gap / 2;
      var h = max > 0 ? (p.value / max) * plotH : 0;
      var hit = svg("rect", { x: m.left + i * band, y: m.top, width: band, height: plotH, "class": "hit", tabindex: "-1" });
      var tip = shortDay(p.label) + ": " + fmt(p.value) + " " + unit;
      hit.addEventListener("mousemove", function (e) { showTip(tip, e); });
      hit.addEventListener("mouseleave", hideTip);
      root.appendChild(hit);
      if (h > 0) {
        var bar = svg("path", { d: barPath(x, m.top + plotH - h, barW, h, 4), "class": "bar" });
        bar.style.pointerEvents = "none";
        root.appendChild(bar);
      }
    });

    root.appendChild(svg("line", { x1: m.left, x2: width - m.right, y1: m.top + plotH, y2: m.top + plotH, "class": "baseline" }));

    var ticks = points.length <= 1 ? [0] : [0, Math.floor((points.length - 1) / 2), points.length - 1];
    ticks.forEach(function (i, n) {
      var anchor = n === 0 ? "start" : (n === ticks.length - 1 ? "end" : "middle");
      var x = n === 0 ? m.left : (n === ticks.length - 1 ? width - m.right : m.left + (i + 0.5) * band);
      var label = svg("text", { x: x, y: height - 6, "text-anchor": anchor, "class": "axis-label" });
      label.textContent = shortDay(points[i].label);
      root.appendChild(label);
    });

    container.appendChild(root);
  }

  function inlineBar(value, max) {
    var root = svg("svg", { viewBox: "0 0 100 10", preserveAspectRatio: "none", "aria-hidden": "true", focusable: "false" });
    root.appendChild(svg("rect", { x: 0, y: 0, width: 100, height: 10, rx: 2, "class": "track" }));
    var w = max > 0 ? Math.max(0, Math.min(100, (value / max) * 100)) : 0;
    if (w > 0) root.appendChild(svg("rect", { x: 0, y: 0, width: w, height: 10, rx: 2, "class": "bar" }));
    return root;
  }

  /**
   * columns: [{key, label, kind: "text"|"code"|"num"|"pct"}], rows: objetos.
   * barKey: coluna numérica que ganha uma barra inline; barMax opcional.
   */
  function table(container, columns, rows, barKey, barMax) {
    clear(container);
    if (!rows.length) {
      container.appendChild(el("p", { className: "empty", text: "Sem dados no período." }));
      return;
    }
    var max = barMax != null ? barMax : Math.max.apply(null, rows.map(function (r) { return r[barKey] || 0; }));
    var head = el("tr", null, columns.map(function (c) {
      return el("th", { scope: "col", className: c.kind === "num" || c.kind === "pct" ? "num" : "", text: c.label });
    }).concat(barKey ? [el("th", { scope: "col" }, [el("span", { className: "visually-hidden", text: "Proporção" })])] : []));
    var body = rows.map(function (r) {
      var cells = columns.map(function (c) {
        var v = r[c.key];
        if (c.kind === "num") return el("td", { className: "num", text: fmt(v) });
        if (c.kind === "pct") return el("td", { className: "num", text: pct(v) });
        return el("td", { className: c.kind === "code" ? "code" : "", text: valueText(v) });
      });
      if (barKey) cells.push(el("td", { className: "barcell" }, [inlineBar(r[barKey] || 0, max)]));
      return el("tr", null, cells);
    });
    container.appendChild(el("table", null, [el("thead", null, [head]), el("tbody", null, body)]));
  }

  function kpis(container, items) {
    clear(container);
    items.forEach(function (item) {
      container.appendChild(el("div", { className: "kpi" }, [
        el("div", { className: "kpi-label", text: item.label }),
        el("div", { className: "kpi-value", text: item.value })
      ]));
    });
  }

  function sectionError(container, error) {
    clear(container);
    container.appendChild(el("p", { className: "error", text: "Erro: " + error.message }));
  }

  // Seções ----------------------------------------------------------------------

  function loadOverview() {
    return api("/v1/stats/overview", range()).then(function (data) {
      var t = data.totals;
      kpis($("overview-kpis"), [
        { label: "Eventos", value: fmt(t.events) },
        { label: "Sessões", value: fmt(t.sessions) },
        { label: "Eventos por sessão", value: t.sessions ? (t.events / t.sessions).toLocaleString("pt-BR", { maximumFractionDigits: 1 }) : "—" }
      ]);
      columnChart($("chart-events"), data.days.map(function (d) { return { label: d.day, value: d.events }; }), "eventos");
      columnChart($("chart-sessions"), data.days.map(function (d) { return { label: d.day, value: d.sessions }; }), "sessões");
      table($("table-days"), [
        { key: "day", label: "Dia", kind: "text" },
        { key: "events", label: "Eventos", kind: "num" },
        { key: "sessions", label: "Sessões", kind: "num" }
      ], data.days.slice().reverse());
      table($("table-top"), [
        { key: "name", label: "Evento", kind: "code" },
        { key: "count", label: "Total", kind: "num" }
      ], data.top_events, "count");
    });
  }

  function loadFunnel() {
    var steps = $("steps").value.trim();
    if (!steps) return Promise.resolve();
    var params = range();
    params.steps = steps;
    return api("/v1/stats/funnel", params).then(function (data) {
      var first = data.steps.length ? data.steps[0].sessions : 0;
      table($("funnel-result"), [
        { key: "step", label: "Passo", kind: "code" },
        { key: "sessions", label: "Sessões", kind: "num" },
        { key: "conversion_from_previous", label: "Do passo anterior", kind: "pct" },
        { key: "conversion_from_first", label: "Do primeiro passo", kind: "pct" }
      ], data.steps, "sessions", first);
    }).catch(function (error) {
      if (error instanceof AuthError) throw error;
      sectionError($("funnel-result"), error);
    });
  }

  function loadBooks() {
    return api("/v1/stats/books", range()).then(function (data) {
      table($("table-books"), [
        { key: "book_id", label: "Livro", kind: "code" },
        { key: "opens", label: "Aberturas", kind: "num" },
        { key: "sessions", label: "Sessões", kind: "num" },
        { key: "completions", label: "Conclusões", kind: "num" },
        { key: "completion_rate", label: "Conclusão", kind: "pct" }
      ], data.books, "completion_rate", 1);
    });
  }

  function loadPaywall() {
    return api("/v1/stats/paywall", range()).then(function (data) {
      kpis($("paywall-kpis"), [
        { label: "Visualizações", value: fmt(data.views_total) },
        { label: "Toques em assinar", value: fmt(data.subscribe_taps_total) },
        { label: "Compras concluídas", value: fmt(data.purchases_completed) },
        { label: "Visualização → compra", value: pct(data.view_to_purchase_rate) }
      ]);
      var valueCols = [{ key: "value", label: "Valor", kind: "code" }, { key: "count", label: "Total", kind: "num" }];
      table($("table-views"), valueCols, data.views_by_source, "count");
      table($("table-closes"), valueCols, data.closes_by_reason, "count");
      table($("table-restores"), valueCols, data.restores_by_result, "count");
      table($("table-gate"), [
        { key: "purpose", label: "Propósito", kind: "code" },
        { key: "shown", label: "Mostrado", kind: "num" },
        { key: "passed", label: "Passou", kind: "num" },
        { key: "failed", label: "Errou", kind: "num" },
        { key: "cancelled", label: "Cancelou", kind: "num" },
        { key: "pass_rate", label: "Taxa de acerto", kind: "pct" }
      ], data.gate, "pass_rate", 1);
      table($("table-purchases"), [
        { key: "plan", label: "Plano", kind: "code" },
        { key: "result", label: "Resultado", kind: "code" },
        { key: "count", label: "Total", kind: "num" }
      ], data.purchases, "count");
    });
  }

  function fillExploreSelectors() {
    var nameSelect = $("explore-name");
    clear(nameSelect);
    Object.keys(catalog.events).sort().forEach(function (name) {
      nameSelect.appendChild(el("option", { value: name, text: name }));
    });
    nameSelect.value = "book_opened";
    fillBySelector();
  }

  function fillBySelector() {
    var bySelect = $("explore-by");
    var event = catalog.events[$("explore-name").value];
    clear(bySelect);
    bySelect.appendChild(el("option", { value: "", text: "(sem quebra)" }));
    var props = el("optgroup", { label: "Propriedades" });
    Object.keys(event ? event.properties : {}).forEach(function (prop) {
      props.appendChild(el("option", { value: prop, text: prop }));
    });
    var ctx = el("optgroup", { label: "Contexto" });
    CONTEXT_FIELDS.forEach(function (field) { ctx.appendChild(el("option", { value: field, text: field })); });
    bySelect.appendChild(props);
    bySelect.appendChild(ctx);
  }

  function loadExplore() {
    var params = range();
    params.name = $("explore-name").value;
    params.by = $("explore-by").value;
    params.group = $("explore-group").value;
    if (!params.name) return Promise.resolve();
    return api("/v1/stats/events", params).then(function (data) {
      var chart = clear($("explore-chart"));
      if (data.group === "day" && !data.by) {
        columnChart(chart, data.rows.map(function (r) { return { label: r.day, value: r.count }; }), data.name);
        table($("explore-table"), [
          { key: "day", label: "Dia", kind: "text" },
          { key: "count", label: "Total", kind: "num" }
        ], data.rows.slice().reverse());
        return;
      }
      var columns = [];
      if (data.group === "day") columns.push({ key: "day", label: "Dia", kind: "text" });
      if (data.by) columns.push({ key: "value", label: data.by, kind: "code" });
      columns.push({ key: "count", label: "Total", kind: "num" });
      table($("explore-table"), columns, data.rows, "count");
    }).catch(function (error) {
      if (error instanceof AuthError) throw error;
      clear($("explore-chart"));
      sectionError($("explore-table"), error);
    });
  }

  function guarded(promise, container) {
    return promise.catch(function (error) {
      if (error instanceof AuthError) throw error;
      if (container) sectionError(container, error);
    });
  }

  function refreshAll() {
    var status = $("status");
    status.textContent = "Carregando…";
    return Promise.all([
      guarded(loadOverview(), $("table-top")),
      loadFunnel(),
      guarded(loadBooks(), $("table-books")),
      guarded(loadPaywall(), $("table-gate")),
      loadExplore()
    ]).then(function () {
      status.textContent = "Atualizado às " + new Date().toLocaleTimeString("pt-BR") + " · período " + $("from").value + " a " + $("to").value + " (UTC)";
    }).catch(function (error) {
      if (error instanceof AuthError) showLogin("Token inválido ou sem acesso.");
      else status.textContent = "Erro: " + error.message;
    });
  }

  function start() {
    showApp();
    var ready = catalog ? Promise.resolve() : api("/v1/catalog").then(function (data) {
      catalog = data;
      fillExploreSelectors();
    });
    return ready.then(refreshAll).catch(function (error) {
      $("status").textContent = "Erro: " + error.message;
    });
  }

  // Datas -----------------------------------------------------------------------

  function isoDay(date) { return date.toISOString().slice(0, 10); }

  function setLastDays(days) {
    var to = new Date();
    var from = new Date(to.getTime() - (days - 1) * 86400000);
    $("to").value = isoDay(to);
    $("from").value = isoDay(from);
  }

  // Início ----------------------------------------------------------------------

  document.addEventListener("DOMContentLoaded", function () {
    tooltip = $("tooltip");
    setLastDays(30);
    $("steps").value = DEFAULT_FUNNEL;

    $("login-form").addEventListener("submit", function (e) {
      e.preventDefault();
      var token = $("token").value.trim();
      if (!token) return;
      setToken(token);
      $("token").value = "";
      start();
    });

    $("logout").addEventListener("click", function () { showLogin(""); });

    $("filters").addEventListener("submit", function (e) {
      e.preventDefault();
      refreshAll();
    });

    Array.prototype.forEach.call(document.querySelectorAll("[data-days]"), function (button) {
      button.addEventListener("click", function () {
        setLastDays(parseInt(button.getAttribute("data-days"), 10));
        refreshAll();
      });
    });

    Array.prototype.forEach.call(document.querySelectorAll("[data-steps]"), function (button) {
      button.addEventListener("click", function () {
        $("steps").value = button.getAttribute("data-steps");
        loadFunnel().catch(function () { showLogin("Token inválido ou sem acesso."); });
      });
    });

    $("funnel-form").addEventListener("submit", function (e) {
      e.preventDefault();
      loadFunnel().catch(function () { showLogin("Token inválido ou sem acesso."); });
    });

    $("explore-name").addEventListener("change", fillBySelector);
    $("explore-form").addEventListener("submit", function (e) {
      e.preventDefault();
      loadExplore().catch(function () { showLogin("Token inválido ou sem acesso."); });
    });

    window.addEventListener("scroll", hideTip, { passive: true });

    if (getToken()) start();
    else showLogin("");
  });
})();
