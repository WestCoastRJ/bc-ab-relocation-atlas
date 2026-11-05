/* BC + Alberta Relocation Atlas
 *
 * Two separate steps:
 *  1. Budget checks (feasibility): rent within budget, airport access when far from Vancouver,
 *     rural land (always "unverified": no free parcel data). Fail = optionally hidden.
 *  2. Preference score: each factor becomes a percentile rank across ALL towns in the atlas
 *     (a fixed comparison group, so toggling layers never changes a town's score).
 *     A missing value counts as the 25th percentile (assumed below average) and is flagged.
 */
(function () {
  "use strict";

  var RAMP = ["#c6dbf3", "#a9c8ee", "#8bb4e8", "#6c9fe0", "#4f89d6", "#3573c8", "#2560b3", "#1a4f9b", "#123f80", "#0b2f63"];
  var FAIL_COLOR = "#c9c8c3";
  var MISSING_RANK = 0.25;
  var STORE_KEY = "atlas-weights-v3";
  var BUDGET_KEY = "atlas-budgets-v1";
  var MODE_KEY = "atlas-ai-mode";
  var INDUSTRY = {
    "11": "Agriculture, forestry, fishing", "21": "Mining, oil & gas", "22": "Utilities", "23": "Construction",
    "31-33": "Manufacturing", "41": "Wholesale", "44-45": "Retail", "48-49": "Transportation & warehousing",
    "51": "Information & media", "52": "Finance & insurance", "53": "Real estate", "54": "Professional services",
    "55": "Management of companies", "56": "Admin & support", "61": "Education", "62": "Health care",
    "71": "Arts & recreation", "72": "Accommodation & food", "81": "Other services", "91": "Public administration"
  };

  var state = {
    data: null, status: null, weights: {}, budgets: {}, aiMode: "gradual",
    scores: {}, ranksByMode: {}, selected: null, map: null, layer: null
  };

  function $(id) { return document.getElementById(id); }
  function money(v) { return typeof v === "number" ? "$" + Math.round(v).toLocaleString() : "n/a"; }
  function fmt(v, c) {
    if (v === null || v === undefined || Number.isNaN(v)) return "missing";
    var n = Math.abs(v) >= 1000 ? Math.round(v).toLocaleString() : (Math.round(v * 10) / 10).toString();
    if (c.unit === "$") return "$" + n;
    if (c.unit === "%") return n + "%";
    return c.unit ? n + " " + c.unit : n;
  }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (ch) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]; }); }

  // ---- AI scenario -----------------------------------------------------------

  function setAiValues(mode) {
    state.data.places.forEach(function (p) {
      var v = p.extra && p.extra["ai_" + mode];
      if (typeof v === "number") p.values.ai = v;
    });
  }

  // ---- budget checks (feasibility) ------------------------------------------

  function checks(p) {
    var e = p.extra || {}, b = state.budgets, out = {};
    var rent = p.values.rent;
    if (typeof rent !== "number") out.rent = { s: "unknown", t: "No CMHC rent survey for this town" };
    else if (rent <= b.rent) out.rent = { s: "pass", t: "2-bed avg " + money(rent) + " within " + money(b.rent) };
    else if (typeof e.rent_1bed === "number" && e.rent_1bed <= b.rent)
      out.rent = { s: "pass", t: "1-bed avg " + money(e.rent_1bed) + " within budget (2-bed " + money(rent) + " is not)" };
    else out.rent = { s: "fail", t: "Avg rent " + money(rent) + " is over " + money(b.rent) };
    if (e.rent_grade && /poor/.test(e.rent_grade) && out.rent.s === "pass") out.rent.t += " (CMHC: poor reliability)";

    out.land = { s: "unverified", t: "No free data on actual land parcels for sale within 20-60 min" };

    if (!e.far_from_vancouver) out.airport = { s: "na", t: "Within ~8 h of Vancouver: airport not needed" };
    else if (typeof e.airport_h !== "number") out.airport = { s: "unknown", t: "Airport access unknown" };
    else if (e.airport_h <= b.airportHours) out.airport = { s: "pass", t: e.airport + ", about " + e.airport_h + " h" };
    else out.airport = { s: "fail", t: e.airport + " is about " + e.airport_h + " h away" };
    return out;
  }

  function fails(p) {
    var c = checks(p);
    return c.rent.s === "fail" || c.airport.s === "fail";
  }

  // ---- scoring ----------------------------------------------------------------

  function percentileRanks(places, key, better) {
    var vals = places.map(function (p) { return { id: p.id, v: p.values[key] }; })
      .filter(function (x) { return typeof x.v === "number" && !Number.isNaN(x.v); })
      .sort(function (a, b) { return a.v - b.v; });
    var out = {}, n = vals.length, i = 0;
    while (i < n) {
      var j = i;
      while (j + 1 < n && vals[j + 1].v === vals[i].v) j++;
      var r = n === 1 ? 1 : ((i + j) / 2) / (n - 1);
      for (var k = i; k <= j; k++) out[vals[k].id] = better === "low" ? 1 - r : r;
      i = j + 1;
    }
    return out;
  }

  function computeScores() {
    var all = state.data.places, crit = state.data.criteria; // fixed comparison group: every town
    var ranks = {};
    crit.forEach(function (c) { ranks[c.key] = percentileRanks(all, c.key, c.better); });
    var scores = {};
    all.forEach(function (p) {
      var sum = 0, wtotal = 0, wmissing = 0, parts = [], missing = [];
      crit.forEach(function (c) {
        var w = state.weights[c.key] || 0;
        if (!w) return;
        wtotal += w;
        var r = ranks[c.key][p.id];
        if (r === undefined) { r = MISSING_RANK; wmissing += w; missing.push(c.label); }
        sum += w * r;
        parts.push({ key: c.key, label: c.label, rank: r, weight: w, missing: ranks[c.key][p.id] === undefined });
      });
      scores[p.id] = { score: wtotal ? sum / wtotal : null, coverage: wtotal ? 1 - wmissing / wtotal : 1, parts: parts, missing: missing };
    });
    return scores;
  }

  // Scores under both AI scenarios, to show how sensitive each town is to that assumption.
  function computeRankRange() {
    var saved = state.aiMode, out = {};
    ["gradual", "fast"].forEach(function (m) {
      setAiValues(m);
      var s = computeScores();
      var order = state.data.places.slice().sort(function (a, b) { return s[b.id].score - s[a.id].score; });
      order.forEach(function (p, i) { (out[p.id] = out[p.id] || {})[m] = i + 1; });
    });
    setAiValues(saved);
    return out;
  }

  function reasons(s) {
    var strong = s.parts.filter(function (x) { return !x.missing; })
      .sort(function (a, b) { return b.weight * b.rank - a.weight * a.rank; }).slice(0, 2)
      .map(function (x) { return x.label.replace(/ \(.*\)$/, ""); });
    var weak = s.parts.filter(function (x) { return !x.missing && x.rank < 0.25; })
      .sort(function (a, b) { return b.weight * (1 - b.rank) - a.weight * (1 - a.rank); }).slice(0, 1)
      .map(function (x) { return x.label.replace(/ \(.*\)$/, ""); });
    return "+ " + strong.join(", ") + (weak.length ? " · − " + weak[0] : "");
  }

  function colorFor(s) {
    if (s === null) return FAIL_COLOR;
    // stretch over the 25-85 range where almost all scores fall, so differences are visible
    var t = Math.max(0, Math.min(0.999, (s - 0.25) / 0.6));
    return RAMP[Math.floor(t * RAMP.length)];
  }

  // ---- UI: controls -------------------------------------------------------------

  function hasData(key) { return state.data.places.some(function (p) { return typeof p.values[key] === "number"; }); }

  function groupTotals() {
    var tot = 0, g = {};
    state.data.criteria.forEach(function (c) { var w = state.weights[c.key] || 0; tot += w; g[c.group] = (g[c.group] || 0) + w; });
    return { tot: tot, g: g };
  }

  function buildSliders() {
    var box = $("sliders"), last = null;
    box.innerHTML = "";
    state.data.criteria.forEach(function (c) {
      if (c.group !== last) {
        var gh = document.createElement("h3");
        gh.className = "group";
        gh.innerHTML = esc(c.group) + ' <span class="gpct" data-group="' + esc(c.group) + '"></span>';
        box.appendChild(gh);
        last = c.group;
      }
      var d = document.createElement("div");
      d.className = "slider" + (hasData(c.key) ? "" : " nodata");
      d.innerHTML = '<div class="row"><label for="w-' + c.key + '">' + esc(c.label) + "</label>" +
        '<span id="wv-' + c.key + '">' + state.weights[c.key] + "</span></div>" +
        '<input type="range" min="0" max="5" step="1" id="w-' + c.key + '" value="' + state.weights[c.key] + '">' +
        '<div class="tag">' + esc(c.hint || "") + "</div>";
      box.appendChild(d);
      d.querySelector("input").addEventListener("input", function (e) {
        state.weights[c.key] = Number(e.target.value);
        $("wv-" + c.key).textContent = e.target.value;
        localStorage.setItem(STORE_KEY, JSON.stringify(state.weights));
        render();
      });
    });
  }

  function updateGroupPct() {
    var t = groupTotals();
    document.querySelectorAll(".gpct").forEach(function (el) {
      var w = t.g[el.getAttribute("data-group")] || 0;
      el.textContent = t.tot ? Math.round(100 * w / t.tot) + "%" : "";
    });
  }

  function defaultWeights() {
    var w = {};
    state.data.criteria.forEach(function (c) { w[c.key] = c.defaultWeight; });
    return w;
  }

  function buildLegend() {
    $("legend").innerHTML = "<strong>Preference score</strong>" +
      '<div class="bar">' + RAMP.map(function (c) { return '<span style="background:' + c + '"></span>'; }).join("") + "</div>" +
      '<div class="ends"><span>25 or less</span><span>85+</span></div>' +
      '<div class="small muted" style="margin-top:4px"><span class="dot fail"></span> fails a budget check · dashed = under 10k people</div>';
  }

  // ---- UI: status ---------------------------------------------------------------

  function renderStatus() {
    var el = $("status"), st = state.status, meta = state.data.sourceMeta || {};
    var lines = [], warn = false;
    if (!st) { lines.push("Refresh status unavailable."); warn = true; }
    else {
      var age = (Date.now() - new Date(st.last_checked).getTime()) / 3600000;
      lines.push("Sources last checked " + new Date(st.last_checked).toLocaleString() + ".");
      if (age > 18) { warn = true; lines.push("The twice-daily refresh appears to have stopped (" + Math.round(age) + " h since last check)."); }
      if (st.degraded) { warn = true; lines.push("Last refresh had problems: " + st.problems.map(esc).join("; ") + ". Older values are shown where needed."); }
    }
    var periods = [];
    if (meta.cmhc) periods.push("rents " + meta.cmhc.reference_period);
    if (meta.business_counts) periods.push("business counts " + meta.business_counts.reference_period);
    periods.push("census May 2021 (income 2020)");
    lines.push("Data periods: " + periods.join(", ") + ". These sources publish once or twice a year.");
    el.className = "status" + (warn ? " warn" : "");
    el.innerHTML = lines.join(" ");
  }

  // ---- UI: map, rankings, detail ------------------------------------------------

  function radiusFor(pop) { return Math.max(4, Math.min(16, 1.6 * Math.sqrt(pop / 1000))); }

  function visible(p) {
    if (!$("showSmall").checked && p.pop2021 < 10000) return false;
    if ($("hideMetroVan").checked && p.region === "Metro Vancouver") return false;
    if ($("hideFails").checked && fails(p)) return false;
    return true;
  }

  var ICON = { pass: "✓", fail: "✗", unknown: "?", unverified: "?", na: "–" };
  function badge(name, c) {
    return '<span class="b ' + c.s + '" title="' + esc(c.t) + '">' + name + " " + ICON[c.s] + "</span>";
  }

  function showDetail(p) {
    state.selected = p.id;
    var s = state.scores[p.id], e = p.extra || {}, ch = checks(p), rr = state.rankRange[p.id] || {};
    var crit = state.data.criteria;
    var rows = crit.map(function (c) {
      var part = s.parts.filter(function (x) { return x.key === c.key; })[0];
      var pct = !state.weights[c.key] ? "off" : part && part.missing ? "assumed 25th" : part ? Math.round(part.rank * 100) + "th pct" : "";
      return '<tr class="' + (state.weights[c.key] ? "" : "off") + '"><td>' + esc(c.label) + '</td><td class="num">' + fmt(p.values[c.key], c) +
        '</td><td class="num muted">' + pct + "</td></tr>";
    }).join("");
    var lines = [];
    if (e.drive_vancouver_h !== undefined) lines.push("About " + e.drive_vancouver_h + " h drive from Vancouver (estimate)");
    if (e.airport) lines.push("Nearest airport: " + esc(e.airport) + ", about " + e.airport_h + " h" + (e.airport_note ? " (" + esc(e.airport_note) + ")" : ""));
    if (typeof p.values.rent === "number") {
      lines.push("CMHC average rent: " + money(e.rent_1bed) + " 1-bed, " + money(p.values.rent) + " 2-bed" +
        (e.rent_source && e.rent_source !== p.name ? " (" + esc(e.rent_source) + ")" : "") +
        (e.rent_grade ? ", reliability " + esc(e.rent_grade) : "") +
        (typeof p.values.vacancy === "number" ? ", vacancy " + p.values.vacancy + "%" : "") + ". Asking rents for newcomers are usually higher.");
    } else lines.push("Rent: no CMHC survey for this town");
    lines.push("Rural land: unverified. " + (e.rural_areas ? "Census proxy pools " + e.rural_areas.length + " rural area(s): " + e.rural_areas.map(esc).join(", ") + " (may be far from town)." : "No census proxy for the surrounding area."));
    if (e.median_income_2020) lines.push("Median employment income (2020): " + money(e.median_income_2020));
    if (e.top_industries) lines.push("Top industries: " + e.top_industries.map(function (t) { return (INDUSTRY[t[0]] || t[0]) + " " + Math.round(t[1]) + "%"; }).join(", "));
    if (e.hands_on_pct !== undefined) lines.push("Hands-on jobs " + e.hands_on_pct + "%, desk/office jobs " + e.desk_pct + "%. AI scenario score " +
      Math.round(e.ai_gradual) + " gradual / " + Math.round(e.ai_fast) + " fast.");
    if (rr.gradual) lines.push("Overall rank: #" + rr.gradual + " with gradual AI, #" + rr.fast + " with fast takeoff (of " + state.data.places.length + ")");
    if (e.businesses) lines.push(e.businesses.toLocaleString() + " businesses with employees, " + e.contractors + " of them building/trade contractors");

    var el = $("detail");
    el.innerHTML = '<button class="close" type="button" aria-label="Close">×</button>' +
      "<h3>" + esc(p.name) + "</h3>" +
      '<div class="muted">' + esc(p.type) + ", " + esc(p.province) + " · " + p.pop2021.toLocaleString() + " people (2021)</div>" +
      (p.pick ? '<div class="pickNote">★ ' + esc(p.pick) + "</div>" : "") +
      '<div class="checks">' + badge("Rent", ch.rent) + badge("Land", ch.land) + badge("Airport", ch.airport) + "</div>" +
      '<div class="checkText">' + [ch.rent, ch.land, ch.airport].map(function (c) { return esc(c.t); }).join(" · ") + "</div>" +
      "<div style='margin-top:6px'><strong>Preference score: " + (s.score === null ? "n/a" : Math.round(s.score * 100) + "/100") + "</strong>" +
      (s.missing.length ? ' <span class="small warnText">missing: ' + s.missing.map(esc).join(", ") + " (counted as below average)</span>" : "") +
      '</div><ul class="extras">' + lines.map(function (l) { return "<li>" + l + "</li>"; }).join("") + "</ul>" +
      "<table>" + rows + "</table>";
    el.hidden = false;
    el.querySelector(".close").onclick = function () { el.hidden = true; state.selected = null; };
  }

  function render() {
    state.scores = computeScores();
    state.rankRange = computeRankRange();
    updateGroupPct();
    var places = state.data.places.filter(visible);
    state.layer.clearLayers();

    places.slice().sort(function (a, b) { return b.pop2021 - a.pop2021; }).forEach(function (p) {
      var s = state.scores[p.id], bad = fails(p);
      var m = L.circleMarker([p.lat, p.lon], {
        radius: radiusFor(p.pop2021), color: "#ffffff", weight: 1.5,
        dashArray: p.pop2021 < 10000 ? "3 3" : null,
        fillColor: bad ? FAIL_COLOR : colorFor(s.score), fillOpacity: p.pop2021 < 10000 ? 0.6 : 0.92
      });
      m.bindTooltip('<div class="tip"><strong>' + esc(p.name) + "</strong><br>" +
        (bad ? "Fails a budget check" : "Score " + Math.round(s.score * 100) + "/100") +
        (s.missing.length ? "<br>" + s.missing.length + " factor(s) missing" : "") + "</div>");
      m.on("click", function () { showDetail(p); });
      m.addTo(state.layer);
      if (p.pick) {
        L.marker([p.lat, p.lon], {
          icon: L.divIcon({ className: "pick", html: '<span class="star">★</span><span class="lbl">' + esc(p.name) + " · your pick</span>", iconSize: [0, 0] }),
          keyboard: false
        }).on("click", function () { showDetail(p); }).addTo(state.layer);
      }
    });

    var ranked = places.filter(function (p) { return !fails(p) && state.scores[p.id].score !== null; })
      .sort(function (a, b) { return state.scores[b.id].score - state.scores[a.id].score; }).slice(0, 25);
    var ol = $("ranking");
    ol.innerHTML = "";
    ranked.forEach(function (p) {
      var s = state.scores[p.id], ch = checks(p), rr = state.rankRange[p.id];
      var li = document.createElement("li");
      li.innerHTML = '<div class="r1">' + (p.pick ? "★ " : "") + esc(p.name) + ' <span class="small muted">' + (p.province === "Alberta" ? "AB" : "BC") +
        (p.pop2021 < 10000 ? " · under 10k" : "") + '</span><span class="score">' + Math.round(s.score * 100) + "</span></div>" +
        '<div class="r2">' + badge("Rent", ch.rent) + badge("Land", ch.land) + badge("Airport", ch.airport) +
        (s.coverage < 1 ? '<span class="b unknown" title="Missing: ' + esc(s.missing.join(", ")) + '">data ' + Math.round(s.coverage * 100) + "%</span>" : "") +
        (rr && Math.abs(rr.gradual - rr.fast) >= 5 ? '<span class="b unknown" title="Rank depends on the AI scenario">AI-sensitive #' + rr.gradual + "/#" + rr.fast + "</span>" : "") +
        '</div><div class="r3 small muted">' + esc(reasons(s)) + "</div>";
      li.onclick = function () { state.map.setView([p.lat, p.lon], 9); showDetail(p); showTab("map"); };
      ol.appendChild(li);
    });
    var hidden = state.data.places.filter(function (p) { return fails(p); }).length;
    $("rankNote").textContent = hidden + " towns fail your budget checks" + ($("hideFails").checked ? " and are hidden." : " (shown grey on the map, left out of this list).");

    if (state.selected) {
      var sel = state.data.places.filter(function (p) { return p.id === state.selected; })[0];
      if (sel) showDetail(sel); // keep an open card in sync with the current settings
    }
  }

  // ---- search + mobile tabs -------------------------------------------------------

  function buildSearch() {
    var dl = $("townList");
    dl.innerHTML = state.data.places.slice().sort(function (a, b) { return a.name.localeCompare(b.name); })
      .map(function (p) { return '<option value="' + esc(p.name) + '">'; }).join("");
    $("search").addEventListener("change", function (e) {
      var q = e.target.value.trim().toLowerCase();
      if (!q) return;
      var p = state.data.places.filter(function (x) { return x.name.toLowerCase() === q; })[0] ||
        state.data.places.filter(function (x) { return x.name.toLowerCase().indexOf(q) === 0; })[0];
      if (!p) return;
      state.map.setView([p.lat, p.lon], 9);
      showDetail(p);
      showTab("map");
      e.target.value = "";
    });
  }

  function showTab(t) {
    document.body.setAttribute("data-tab", t);
    document.querySelectorAll("#tabs button").forEach(function (b) { b.classList.toggle("on", b.getAttribute("data-tab") === t); });
    if (t === "map" && state.map) setTimeout(function () { state.map.invalidateSize(); }, 50);
  }

  // ---- init -------------------------------------------------------------------------

  function init(data, status) {
    state.data = data;
    state.status = status;
    var saved = null, savedB = null;
    try { saved = JSON.parse(localStorage.getItem(STORE_KEY)); savedB = JSON.parse(localStorage.getItem(BUDGET_KEY)); } catch (e) { /* ignore */ }
    state.weights = Object.assign(defaultWeights(), saved || {});
    state.budgets = Object.assign({}, data.budgets, savedB || {});
    state.aiMode = localStorage.getItem(MODE_KEY) === "fast" ? "fast" : "gradual";
    setAiValues(state.aiMode);

    $("budgetRent").value = state.budgets.rent;
    $("budgetAir").value = state.budgets.airportHours;
    [["budgetRent", "rent"], ["budgetAir", "airportHours"]].forEach(function (x) {
      $(x[0]).addEventListener("change", function (e) {
        var v = Number(e.target.value);
        if (v > 0) { state.budgets[x[1]] = v; localStorage.setItem(BUDGET_KEY, JSON.stringify(state.budgets)); render(); }
      });
    });
    document.querySelectorAll('input[name="aiMode"]').forEach(function (r) {
      r.checked = r.value === state.aiMode;
      r.addEventListener("change", function () {
        state.aiMode = r.value;
        localStorage.setItem(MODE_KEY, r.value);
        setAiValues(r.value);
        render();
      });
    });

    state.map = L.map("map", { zoomControl: true });
    state.map.fitBounds([[48.3, -131], [59.5, -110]]);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 13, attribution: "&copy; OpenStreetMap contributors" }).addTo(state.map);
    state.layer = L.layerGroup().addTo(state.map);

    $("sources").innerHTML = Object.keys(data.sourceMeta || {}).map(function (k) {
      var m = data.sourceMeta[k];
      return "<li>" + esc(m.label) + (m.reference_period ? " (" + esc(m.reference_period) + ")" : "") + "</li>";
    }).join("");
    renderStatus();
    buildSliders();
    buildLegend();
    buildSearch();
    ["showSmall", "hideMetroVan", "hideFails"].forEach(function (id) { $(id).addEventListener("change", render); });
    $("reset").addEventListener("click", function () {
      state.weights = defaultWeights();
      localStorage.removeItem(STORE_KEY);
      buildSliders();
      render();
    });
    document.querySelectorAll("#tabs button").forEach(function (b) { b.onclick = function () { showTab(b.getAttribute("data-tab")); }; });
    showTab("map");
    render();
  }

  Promise.all([
    fetch("data/places.json", { cache: "no-store" }).then(function (r) { return r.json(); }),
    fetch("data/status.json", { cache: "no-store" }).then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; })
  ]).then(function (x) { init(x[0], x[1]); })
    .catch(function (e) { $("status").textContent = "Could not load data: " + e.message; });
})();
