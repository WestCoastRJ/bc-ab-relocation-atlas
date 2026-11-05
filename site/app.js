/* BC + Alberta Relocation Atlas
 * Loads data/places.json, scores every place from the user's slider weights,
 * and colours the map. Each criterion is turned into a 0..1 percentile rank
 * across the places that have data for it, so outliers can't swamp the score.
 */
(function () {
  "use strict";

  // Sequential blue ramp: low match = light, high match = dark.
  var RAMP = ["#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281"];
  var NO_DATA = "#c9c8c3";
  var STORE_KEY = "atlas-weights-v2";
  var MODE_KEY = "atlas-ai-mode";
  var FAST_AI_BOOST = 1.5; // in fast-takeoff mode the AI factor counts 50% more
  var INDUSTRY = {
    "11": "Agriculture, forestry, fishing", "21": "Mining, oil & gas", "22": "Utilities", "23": "Construction",
    "31-33": "Manufacturing", "41": "Wholesale", "44-45": "Retail", "48-49": "Transportation & warehousing",
    "51": "Information & media", "52": "Finance & insurance", "53": "Real estate", "54": "Professional services",
    "55": "Management of companies", "56": "Admin & support", "61": "Education", "62": "Health care",
    "71": "Arts & recreation", "72": "Accommodation & food", "81": "Other services", "91": "Public administration"
  };

  var state = { data: null, weights: {}, markers: {}, scores: {}, map: null, layer: null, aiMode: "gradual" };

  function applyAiMode() {
    state.data.places.forEach(function (p) {
      var v = p.extra && p.extra["ai_" + state.aiMode];
      if (typeof v === "number") p.values.ai = v;
    });
  }

  function effWeight(key) {
    var w = state.weights[key] || 0;
    return key === "ai" && state.aiMode === "fast" ? w * FAST_AI_BOOST : w;
  }

  function fmt(v, c) {
    if (v === null || v === undefined || Number.isNaN(v)) return "no data";
    var n = Math.abs(v) >= 1000 ? Math.round(v).toLocaleString() : (Math.round(v * 10) / 10).toString();
    if (c.unit === "$") return "$" + n;
    if (c.unit === "%") return n + "%";
    return c.unit ? n + " " + c.unit : n;
  }

  // ---- scoring -------------------------------------------------------------

  function percentileRanks(places, key, better) {
    var vals = places
      .map(function (p) { return { id: p.id, v: p.values[key] }; })
      .filter(function (x) { return typeof x.v === "number" && !Number.isNaN(x.v); })
      .sort(function (a, b) { return a.v - b.v; });
    var out = {};
    var n = vals.length;
    if (n === 0) return out;
    // ties share the average rank
    var i = 0;
    while (i < n) {
      var j = i;
      while (j + 1 < n && vals[j + 1].v === vals[i].v) j++;
      var r = n === 1 ? 1 : ((i + j) / 2) / (n - 1);
      for (var k = i; k <= j; k++) out[vals[k].id] = better === "low" ? 1 - r : r;
      i = j + 1;
    }
    return out;
  }

  function visiblePlaces() {
    var showSmall = document.getElementById("showSmall").checked;
    var hideMV = document.getElementById("hideMetroVan").checked;
    return state.data.places.filter(function (p) {
      if (!showSmall && p.pop2021 < 10000) return false;
      if (hideMV && p.region === "Metro Vancouver") return false;
      return true;
    });
  }

  function computeScores(places) {
    var crit = state.data.criteria;
    var ranks = {};
    crit.forEach(function (c) { ranks[c.key] = percentileRanks(places, c.key, c.better); });
    var scores = {};
    places.forEach(function (p) {
      var sum = 0, wsum = 0, wtotal = 0, parts = [];
      crit.forEach(function (c) {
        var w = effWeight(c.key);
        if (!w) return;
        wtotal += w;
        var r = ranks[c.key][p.id];
        if (r === undefined) return;
        sum += w * r; wsum += w;
        parts.push({ key: c.key, rank: r, weight: w });
      });
      scores[p.id] = {
        score: wsum ? sum / wsum : null,
        coverage: wtotal ? wsum / wtotal : 0,
        parts: parts
      };
    });
    return scores;
  }

  function colorFor(s) {
    if (s === null) return NO_DATA;
    return RAMP[Math.min(RAMP.length - 1, Math.floor(s * RAMP.length))];
  }

  // ---- UI ------------------------------------------------------------------

  function hasData(key) {
    return state.data.places.some(function (p) { return typeof p.values[key] === "number"; });
  }

  function buildSliders() {
    var box = document.getElementById("sliders");
    box.innerHTML = "";
    var lastGroup = null;
    state.data.criteria.forEach(function (c) {
      if (c.group && c.group !== lastGroup) {
        var gh = document.createElement("h3");
        gh.className = "group";
        gh.textContent = c.group;
        box.appendChild(gh);
        lastGroup = c.group;
      }
      var d = document.createElement("div");
      var ok = hasData(c.key);
      d.className = "slider" + (ok ? "" : " nodata");
      d.innerHTML =
        '<div class="row"><label for="w-' + c.key + '">' + c.label + "</label>" +
        '<span id="wv-' + c.key + '">' + state.weights[c.key] + "</span></div>" +
        '<input type="range" min="0" max="5" step="1" id="w-' + c.key + '" value="' + state.weights[c.key] + '">' +
        '<div class="tag">' + (ok ? c.hint || "" : "Data coming soon") + "</div>";
      box.appendChild(d);
      d.querySelector("input").addEventListener("input", function (e) {
        state.weights[c.key] = Number(e.target.value);
        document.getElementById("wv-" + c.key).textContent = e.target.value;
        localStorage.setItem(STORE_KEY, JSON.stringify(state.weights));
        render();
      });
    });
  }

  function defaultWeights() {
    var w = {};
    state.data.criteria.forEach(function (c) { w[c.key] = c.defaultWeight; });
    return w;
  }

  function buildLegend() {
    var el = document.getElementById("legend");
    el.innerHTML = "<strong>Match score</strong>" +
      '<div class="bar">' + RAMP.map(function (c) { return '<span style="background:' + c + '"></span>'; }).join("") + "</div>" +
      '<div class="ends"><span>weaker</span><span>stronger</span></div>' +
      '<div class="small muted" style="margin-top:4px"><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:' + NO_DATA + '"></span> not enough data · dashed = under 10k</div>';
  }

  function radiusFor(pop) {
    return Math.max(5, Math.min(22, 2.2 * Math.sqrt(pop / 1000)));
  }

  function showDetail(p) {
    var s = state.scores[p.id] || {};
    var el = document.getElementById("detail");
    var rows = state.data.criteria.map(function (c) {
      var part = (s.parts || []).filter(function (x) { return x.key === c.key; })[0];
      var pct = part ? Math.round(part.rank * 100) + "th pct" : "";
      return "<tr><td>" + c.label + '</td><td class="num">' + fmt(p.values[c.key], c) +
        '</td><td class="num muted">' + pct + "</td></tr>";
    }).join("");
    el.innerHTML =
      '<button class="close" type="button" aria-label="Close">×</button>' +
      "<h3>" + p.name + "</h3>" +
      '<div class="muted">' + p.type + ", " + p.province + " · " + p.pop2021.toLocaleString() + " people (2021)</div>" +
      "<div style='margin-top:6px'><strong>Match: " + (s.score === null || s.score === undefined ? "n/a" : Math.round(s.score * 100) + "/100") + "</strong>" +
      (s.coverage < 1 ? ' <span class="muted small">(' + Math.round(s.coverage * 100) + "% of your weighted factors have data)</span>" : "") +
      "</div>" + (p.pick ? '<div class="pickNote">★ ' + p.pick + "</div>" : "") +
      extrasHtml(p) + "<table>" + rows + "</table>";
    el.hidden = false;
    el.querySelector(".close").onclick = function () { el.hidden = true; };
  }

  function money(v) { return typeof v === "number" ? "$" + Math.round(v).toLocaleString() : "n/a"; }

  function extrasHtml(p) {
    var e = p.extra || {};
    var lines = [];
    if (e.drive_vancouver_h !== undefined) lines.push("About " + e.drive_vancouver_h + " h drive from Vancouver");
    if (e.airport) lines.push("Nearest airport: " + e.airport + " (" + e.airport_h + " h)");
    if (p.values.rent) lines.push("Rent: " + money(e.rent_1bed) + " 1-bed, " + money(p.values.rent) + " 2-bed" +
      (e.rent_source && e.rent_source !== p.name ? " (" + e.rent_source + ")" : ""));
    else lines.push("Rent: no CMHC survey for this town");
    if (e.median_income) lines.push("Median employment income: " + money(e.median_income));
    if (e.top_industries) lines.push("Top industries: " + e.top_industries.map(function (t) {
      return (INDUSTRY[t[0]] || t[0]) + " " + Math.round(t[1]) + "%";
    }).join(", "));
    if (e.hands_on_pct !== undefined) lines.push("Workers in hands-on jobs: " + e.hands_on_pct + "% · desk/office jobs: " + e.desk_pct + "%");
    if (e.ai_gradual !== undefined) lines.push("AI resistance: " + Math.round(e.ai_gradual) + "/100 gradual, " + Math.round(e.ai_fast) + "/100 fast takeoff");
    if (e.businesses) lines.push(e.businesses.toLocaleString() + " businesses with employees, " + e.contractors + " building/trade contractors");
    if (!p.values.rural_value) lines.push("Rural property: no census data for the surrounding rural area");
    return '<ul class="extras">' + lines.map(function (l) { return "<li>" + l + "</li>"; }).join("") + "</ul>";
  }

  function render() {
    var places = visiblePlaces();
    state.scores = computeScores(places);
    state.layer.clearLayers();

    var mapped = places.filter(function (p) { return typeof p.lat === "number"; });
    mapped
      .slice()
      .sort(function (a, b) { return b.pop2021 - a.pop2021; }) // small dots on top
      .forEach(function (p) {
        var s = state.scores[p.id];
        var m = L.circleMarker([p.lat, p.lon], {
          radius: radiusFor(p.pop2021),
          color: "#fcfcfb", weight: 2,            // surface ring so overlaps stay readable
          dashArray: p.pop2021 < 10000 ? "3 3" : null,
          fillColor: colorFor(s.score), fillOpacity: p.pop2021 < 10000 ? 0.55 : 0.9
        });
        m.bindTooltip('<div class="tip"><strong>' + p.name + "</strong><br>" +
          (s.score === null ? "not enough data" : "Match " + Math.round(s.score * 100) + "/100") +
          "<br>" + p.pop2021.toLocaleString() + " people</div>");
        m.on("click", function () { showDetail(p); });
        m.addTo(state.layer);
        if (p.pick) {
          var star = L.marker([p.lat, p.lon], {
            icon: L.divIcon({ className: "pick", html: '<span class="star">★</span><span class="lbl">' + p.name + " · your pick</span>", iconSize: [0, 0] }),
            keyboard: false
          });
          star.on("click", function () { showDetail(p); });
          star.addTo(state.layer);
        }
        state.markers[p.id] = m;
      });

    var ranked = places
      .filter(function (p) { return state.scores[p.id].score !== null; })
      .sort(function (a, b) { return state.scores[b.id].score - state.scores[a.id].score; })
      .slice(0, 15);
    var ol = document.getElementById("ranking");
    ol.innerHTML = "";
    ranked.forEach(function (p) {
      var li = document.createElement("li");
      li.innerHTML = (p.pick ? "★ " : "") + p.name + ' <span class="small muted">' + (p.province === "Alberta" ? "AB" : "BC") +
        '</span><span class="score">' + Math.round(state.scores[p.id].score * 100) + "</span>";
      li.onclick = function () {
        if (typeof p.lat === "number") state.map.setView([p.lat, p.lon], 9);
        showDetail(p);
      };
      ol.appendChild(li);
    });
  }

  function init(data) {
    state.data = data;
    var saved = null;
    try { saved = JSON.parse(localStorage.getItem(STORE_KEY)); } catch (e) { /* ignore */ }
    state.weights = Object.assign(defaultWeights(), saved || {});
    state.aiMode = localStorage.getItem(MODE_KEY) === "fast" ? "fast" : "gradual";
    applyAiMode();
    document.querySelectorAll('input[name="aiMode"]').forEach(function (r) {
      r.checked = r.value === state.aiMode;
      r.addEventListener("change", function () {
        state.aiMode = r.value;
        localStorage.setItem(MODE_KEY, r.value);
        applyAiMode();
        render();
      });
    });
    document.getElementById("sources").innerHTML = (data.sources || []).map(function (s) { return "<li>" + s + "</li>"; }).join("") +
      (data.notes || []).map(function (s) { return '<li class="warn">' + s + "</li>"; }).join("");

    document.getElementById("updated").textContent =
      "Data refreshed " + new Date(data.updated).toLocaleString() + " · " + data.places.length + " towns";

    state.map = L.map("map", { zoomControl: true }).setView([53.5, -120], 5);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 13,
      attribution: "&copy; OpenStreetMap contributors"
    }).addTo(state.map);
    state.layer = L.layerGroup().addTo(state.map);

    buildSliders();
    buildLegend();
    ["showSmall", "hideMetroVan"].forEach(function (id) {
      document.getElementById(id).addEventListener("change", render);
    });
    document.getElementById("reset").addEventListener("click", function () {
      state.weights = defaultWeights();
      localStorage.removeItem(STORE_KEY);
      buildSliders();
      render();
    });
    render();
  }

  fetch("data/places.json", { cache: "no-store" })
    .then(function (r) { return r.json(); })
    .then(init)
    .catch(function (e) {
      document.getElementById("updated").textContent = "Could not load data: " + e.message;
    });
})();
