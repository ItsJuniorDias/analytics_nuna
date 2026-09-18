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
  var CONTEXT_FIELDS = ["app_version", "build", "os_version", "device_family", "storefront", "layout", "subscription_state"];
  var DEFAULT_FUNNEL = "paywall_viewed,subscribe_tapped,parental_gate_shown:purpose=subscribe,parental_gate_passed:purpose=subscribe,purchase_completed";

  var memoryToken = null; // se sessionStorage estiver bloqueado
  var catalog = null;
  var numberFormat = new Intl.NumberFormat("pt-BR");
  var percentFormat = new Intl.NumberFormat("pt-BR", { style: "percent", maximumFractionDigits: 1 });
  var monthNames = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"];

  function $(id) { return document.getElementById(id); }

  // Países ------------------------------------------------------------------
  //
  // O app manda o país da conta da App Store em ISO 3166-1 alfa-3 (é o que o
  // StoreKit dá: "BRA"). Bandeira e nome precisam do alfa-2 ("BR"). Tabela
  // tirada do ICU (uloc_getISO3Country), 5 letras por país: alfa-3 + alfa-2.
  var ISO3_TO_ISO2 = (function () {
    var packed =
    "ABWAW AFGAF AGOAO AIAAI ALAAX ALBAL ANDAD AREAE ARGAR ARMAM ASCAC ASMAS ATAAQ ATFTF ATGAG AUSAU " +
    "AUTAT AZEAZ BDIBI BELBE BENBJ BESBQ BFABF BGDBD BGRBG BHRBH BHSBS BIHBA BLMBL BLRBY BLZBZ BMUBM " +
    "BOLBO BRABR BRBBB BRNBN BTNBT BVTBV BWABW CAFCF CANCA CCKCC CHECH CHLCL CHNCN CIVCI CMRCM CODCD " +
    "COGCG COKCK COLCO COMKM CPTCP CPVCV CRICR CRQCQ CUBCU CUWCW CXRCX CYMKY CYPCY CZECZ DEUDE DGADG " +
    "DJIDJ DMADM DNKDK DOMDO DZADZ ECUEC EGYEG ERIER ESHEH ESPES ESTEE ETHET FINFI FJIFJ FLKFK FRAFR " +
    "FROFO FSMFM GABGA GBRGB GEOGE GGYGG GHAGH GIBGI GINGN GLPGP GMBGM GNBGW GNQGQ GRCGR GRDGD GRLGL " +
    "GTMGT GUFGF GUMGU GUYGY HKGHK HMDHM HNDHN HRVHR HTIHT HUNHU IDNID IMNIM INDIN IOTIO IRLIE IRNIR " +
    "IRQIQ ISLIS ISRIL ITAIT JAMJM JEYJE JORJO JPNJP KAZKZ KENKE KGZKG KHMKH KIRKI KNAKN KORKR KWTKW " +
    "LAOLA LBNLB LBRLR LBYLY LCALC LIELI LKALK LSOLS LTULT LUXLU LVALV MACMO MAFMF MARMA MCOMC MDAMD " +
    "MDGMG MDVMV MEXMX MHLMH MKDMK MLIML MLTMT MMRMM MNEME MNGMN MNPMP MOZMZ MRTMR MSRMS MTQMQ MUSMU " +
    "MWIMW MYSMY MYTYT NAMNA NCLNC NERNE NFKNF NGANG NICNI NIUNU NLDNL NORNO NPLNP NRUNR NZLNZ OMNOM " +
    "PAKPK PANPA PCNPN PERPE PHLPH PLWPW PNGPG POLPL PRIPR PRKKP PRTPT PRYPY PSEPS PYFPF QATQA REURE " +
    "ROURO RUSRU RWARW SAUSA SDNSD SENSN SGPSG SGSGS SHNSH SJMSJ SLBSB SLESL SLVSV SMRSM SOMSO SPMPM " +
    "SRBRS SSDSS STPST SURSR SVKSK SVNSI SWESE SWZSZ SXMSX SYCSC SYRSY TAATA TCATC TCDTD TGOTG THATH " +
    "TJKTJ TKLTK TKMTM TLSTL TONTO TTOTT TUNTN TURTR TUVTV TWNTW TZATZ UGAUG UKRUA UMIUM URYUY USAUS " +
    "UZBUZ VATVA VCTVC VENVE VGBVG VIRVI VNMVN VUTVU WLFWF WSMWS XEAEA XICIC XKKXK YEMYE ZAFZA ZMBZM " +
    "ZWEZW";
    var map = {};
    packed.split(" ").forEach(function (item) { map[item.slice(0, 3)] = item.slice(3); });
    return map;
  })();

  var regionNames = null;
  try { regionNames = new Intl.DisplayNames(["pt-BR"], { type: "region" }); } catch (e) { /* navegador antigo: fica o código */ }

  /** Bandeira em emoji: cada letra do alfa-2 vira um "regional indicator". */
  function flag(iso2) {
    return String.fromCodePoint(0x1F1E6 + iso2.charCodeAt(0) - 65, 0x1F1E6 + iso2.charCodeAt(1) - 65);
  }

  /** "BRA" -> "🇧🇷 Brasil"; sem país -> "🏳️ Não informado". */
  function countryText(iso3) {
    if (iso3 === null || iso3 === undefined) return "\u{1F3F3}\uFE0F Não informado";
    var iso2 = ISO3_TO_ISO2[iso3];
    if (!iso2) return iso3;
    var name = iso2;
    try { if (regionNames) name = regionNames.of(iso2) || iso2; } catch (e) { /* fica o código */ }
    return flag(iso2) + " " + name;
  }

  // Conversão: rótulos --------------------------------------------------------

  var STEP_LABELS = {
    viewed: "Viu o paywall",
    subscribe_tapped: "Tocou em assinar",
    gate_passed: "Passou pelo portão dos pais",
    purchased: "Comprou"
  };

  var UNMEASURED = "Instalação anterior à medição";

  // Valores dos enums do catálogo em português. O que não estiver aqui aparece
  // como veio (versão do app, por exemplo).
  var SEGMENT_LABELS = {
    source: { post_onboarding: "Depois do onboarding", home_header: "Cabeçalho da Home", locked_book: "Livro bloqueado", parents: "Área dos pais" },
    trial_eligible: { "true": "Elegível", "false": "Não elegível" },
    install_age_bucket: { lt_1d: "Menos de 1 dia", "1_3d": "1 a 3 dias", "3_7d": "3 a 7 dias", "7_30d": "7 a 30 dias", "30_90d": "30 a 90 dias", gte_90d: "90 dias ou mais", unknown: UNMEASURED },
    prior_paywall_views_bucket: { "0": "Primeira vez", "1": "1 antes", "2_4": "2 a 4 antes", "5_9": "5 a 9 antes", gte_10: "10 ou mais antes", unknown: UNMEASURED },
    books_completed_bucket: { "0": "Nenhum", "1": "1", "2_4": "2 a 4", "5_9": "5 a 9", gte_10: "10 ou mais", unknown: UNMEASURED },
    device_family: { phone: "iPhone", pad: "iPad", other: "Outro" }
  };

  function segmentText(by, value) {
    if (value === null || value === undefined) {
      // Faixa da jornada ausente: evento de uma versão do app sem a medição.
      return /_bucket$/.test(by) ? "Versão do app sem a medição" : "(ausente)";
    }
    var labels = SEGMENT_LABELS[by];
    var key = String(value);
    return labels && labels[key] ? labels[key] : key;
  }

  /** "2026-09-18T14:03:22.123Z" -> "18 set 14:03" (UTC, como o resto do painel). */
  function whenText(ts) {
    return shortDay(ts.slice(0, 10)) + " " + ts.slice(11, 16);
  }

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
    $("clear-events").hidden = true;
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
    $("clear-events").hidden = false;
  }

  // API -----------------------------------------------------------------------

  function AuthError() { this.name = "AuthError"; }

  function api(path, params, method) {
    var url = new URL(path, window.location.origin);
    Object.keys(params || {}).forEach(function (key) {
      if (params[key] !== undefined && params[key] !== null && params[key] !== "") {
        url.searchParams.set(key, params[key]);
      }
    });
    var headers = {};
    var token = getToken();
    if (token) headers.Authorization = "Bearer " + token;
    return fetch(url.toString(), { method: method || "GET", headers: headers, cache: "no-store", credentials: "omit" })
      .then(function (res) {
        if (res.status === 401 && (path.indexOf("/v1/stats") === 0 || path.indexOf("/v1/admin") === 0)) throw new AuthError();
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
        if (c.kind === "country") return el("td", { className: "country", text: countryText(v), title: v == null ? "" : v });
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
      table($("table-countries"), [
        { key: "storefront", label: "País", kind: "country" },
        { key: "sessions", label: "Sessões", kind: "num" },
        { key: "events", label: "Eventos", kind: "num" }
      ], data.storefronts, "sessions");
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

  // Conversão do paywall --------------------------------------------------------

  function loadConversion() {
    var params = range();
    params.by = $("segment-by").value;
    return api("/v1/stats/paywall/conversion", params).then(function (data) {
      var steps = data.steps;
      var first = steps[0].sessions;
      var bought = steps[steps.length - 1];
      kpis($("conversion-kpis"), [
        { label: "Sessões que viram o paywall", value: fmt(first) },
        { label: "Tocaram em assinar", value: pct(steps[1].conversion_from_first) },
        { label: "Compraram", value: fmt(bought.sessions) },
        { label: "Conversão", value: pct(bought.conversion_from_first) }
      ]);
      table($("table-conversion-steps"), [
        { key: "label", label: "Passo", kind: "text" },
        { key: "sessions", label: "Sessões", kind: "num" },
        { key: "conversion_from_previous", label: "Do passo anterior", kind: "pct" },
        { key: "conversion_from_first", label: "Do início", kind: "pct" }
      ], steps.map(function (s) { return Object.assign({ label: STEP_LABELS[s.key] || s.key }, s); }), "sessions", first);

      var select = $("segment-by");
      var isCountry = data.by === "storefront";
      table($("table-segments"), [
        { key: "label", label: select.options[select.selectedIndex].textContent, kind: isCountry ? "country" : "text" },
        { key: "sessions", label: "Sessões", kind: "num" },
        { key: "subscribe_tapped", label: "Tocaram em assinar", kind: "num" },
        { key: "purchased", label: "Compraram", kind: "num" },
        { key: "conversion", label: "Conversão", kind: "pct" }
      ], data.segments.map(function (s) {
        return Object.assign({ label: isCountry ? s.value : segmentText(data.by, s.value) }, s);
      }), "conversion");
    });
  }

  // Sessões ---------------------------------------------------------------------

  var SESSION_LIMIT = 50;

  function loadSessions() {
    var params = range();
    params.outcome = $("sessions-outcome").value;
    params.limit = SESSION_LIMIT;
    return api("/v1/stats/paywall/sessions", params).then(function (data) {
      var container = clear($("table-sessions"));
      if (!data.sessions.length) {
        container.appendChild(el("p", { className: "empty", text: "Nenhuma sessão no período." }));
        return;
      }
      var head = el("tr", null, ["Sessão", "Quando (UTC)", "País", "Origem", "Instalação", "Paywalls antes", "Chegou até"].map(function (label) {
        return el("th", { scope: "col", text: label });
      }));
      // A API não manda id nenhum: a sessão é só a posição dela nesta lista.
      var body = data.sessions.map(function (s, index) {
        var number = index + 1;
        var open = el("button", { className: "button quiet compact", type: "button", text: "#" + number, "aria-label": "Ver os eventos da sessão " + number });
        open.addEventListener("click", function () { showTimeline(s, number); });
        var furthest = STEP_LABELS[s.furthest_step] || s.furthest_step;
        return el("tr", null, [
          el("td", null, [open]),
          el("td", { text: whenText(s.first_view_at) }),
          el("td", { className: "country", text: countryText(s.storefront), title: s.storefront || "" }),
          el("td", { text: segmentText("source", s.source) }),
          el("td", { text: segmentText("install_age_bucket", s.install_age_bucket) }),
          el("td", { text: segmentText("prior_paywall_views_bucket", s.prior_paywall_views_bucket) }),
          el("td", { className: s.purchased ? "bought" : "", text: s.purchased ? "✓ " + furthest : furthest })
        ]);
      });
      container.appendChild(el("table", null, [el("thead", null, [head]), el("tbody", null, body)]));
      if (data.total > data.sessions.length) {
        container.appendChild(el("p", { className: "muted", text: "As " + fmt(data.sessions.length) + " mais recentes de " + fmt(data.total) + "." }));
      }
    });
  }

  function propsText(props) {
    var keys = Object.keys(props || {}).sort();
    if (!keys.length) return "—";
    return keys.map(function (key) { return key + "=" + props[key]; }).join(" · ");
  }

  function showTimeline(session, number) {
    $("session-detail").hidden = false;
    $("session-title").textContent = "Sessão #" + number;
    var shown = session.events.length;
    $("session-meta").textContent = [
      countryText(session.storefront),
      segmentText("device_family", session.device_family),
      "versão " + session.app_version,
      fmt(session.event_count) + " eventos" + (shown < session.event_count ? " (mostrando " + fmt(shown) + ")" : "")
    ].join(" · ");
    table($("table-timeline"), [
      { key: "time", label: "Hora (UTC)", kind: "text" },
      { key: "name", label: "Evento", kind: "code" },
      { key: "details", label: "Propriedades", kind: "code" },
      { key: "subscription_state", label: "Assinatura", kind: "code" }
    ], session.events.map(function (e) {
      return { time: e.ts.slice(11, 19), name: e.name, details: propsText(e.properties), subscription_state: e.subscription_state };
    }));
    $("session-title").focus();
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
      if (data.by) columns.push({ key: "value", label: data.by === "storefront" ? "País" : data.by, kind: data.by === "storefront" ? "country" : "code" });
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

  // Limpar eventos ----------------------------------------------------------------

  /** Apaga TODOS os eventos no servidor. Pede para digitar LIMPAR: um clique
   *  sem querer não pode zerar o painel. */
  function clearEvents() {
    var status = $("status");
    var answer = window.prompt(
      "Isso apaga TODOS os eventos do servidor, de todos os períodos, e não tem volta.\n\n" +
      "Digite LIMPAR para confirmar."
    );
    if (answer === null) return;
    if (answer.trim().toUpperCase() !== "LIMPAR") {
      status.textContent = "Nada foi apagado: a confirmação não bateu.";
      return;
    }
    var button = $("clear-events");
    button.disabled = true;
    status.textContent = "Apagando eventos…";
    api("/v1/admin/events", null, "DELETE").then(function (data) {
      return refreshAll().then(function () {
        status.textContent = fmt(data.deleted_events) + " eventos apagados.";
      });
    }).catch(function (error) {
      if (error instanceof AuthError) showLogin("Token inválido ou sem acesso.");
      else status.textContent = "Erro ao apagar: " + error.message;
    }).then(function () {
      button.disabled = false;
    });
  }

  function refreshAll() {
    var status = $("status");
    status.textContent = "Carregando…";
    // Outro período: a sessão aberta pode nem estar na lista nova.
    $("session-detail").hidden = true;
    // Na ordem da página: conversão do paywall primeiro, a seção mais importante.
    return Promise.all([
      guarded(loadConversion(), $("table-segments")),
      guarded(loadSessions(), $("table-sessions")),
      guarded(loadPaywall(), $("table-gate")),
      guarded(loadOverview(), $("table-top")),
      loadFunnel(),
      guarded(loadBooks(), $("table-books")),
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
    $("clear-events").addEventListener("click", clearEvents);

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

    $("segment-by").addEventListener("change", function () {
      guarded(loadConversion(), $("table-segments")).catch(function () { showLogin("Token inválido ou sem acesso."); });
    });
    $("sessions-outcome").addEventListener("change", function () {
      $("session-detail").hidden = true;
      guarded(loadSessions(), $("table-sessions")).catch(function () { showLogin("Token inválido ou sem acesso."); });
    });
    ["segment-form", "sessions-form"].forEach(function (id) {
      $(id).addEventListener("submit", function (e) { e.preventDefault(); });
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
