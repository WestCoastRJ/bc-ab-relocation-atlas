/* BC + Alberta Relocation Atlas
 *
 * Two separate steps:
 *  1. Budget checks (feasibility): rent within budget, airport access when far from Vancouver,
 *     rural land (always "unverified": assessed values, not parcels for sale). Fail = optionally hidden.
 *  2. Preference score: each factor becomes a percentile rank across ALL towns in the atlas
 *     (a fixed comparison group, so toggling layers never changes a town's score).
 *     A missing value counts as the 25th percentile (assumed below average) and is flagged.
 */
(function () {
  "use strict";

  // Five clearly different tiers (ColorBrewer RdYlBu: readable with red-green colour blindness).
  // Tiers are quintiles of the towns that pass the budget checks, so every colour is always in use.
  var TIERS = [
    { color: "#d73027", label: "Weakest 20%" },
    { color: "#fc8d59", label: "Below average" },
    { color: "#fee090", label: "Middle" },
    { color: "#91bfdb", label: "Above average" },
    { color: "#4575b4", label: "Best 20%" }
  ];
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
  function ordinal(n) {
    var t = n % 100, u = n % 10;
    return n + (t >= 11 && t <= 13 ? "th" : u === 1 ? "st" : u === 2 ? "nd" : u === 3 ? "rd" : "th");
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
    var rent = p.values.rent, est = isEstimate(p), rr = e.rent_range;
    if (typeof rent !== "number") out.rent = { s: "unknown", t: "No rent survey or estimate for this town" };
    else if (est) {
      var wide = e.rent_range_wide, top = wide ? wide[1] : rr ? rr[1] : rent;
      var rng = rr ? " (if it compares like the middle half of other BC towns: " + money(rr[0]) + " to " + money(rr[1]) +
        (wide ? "; full spread " + money(wide[0]) + " to " + money(wide[1]) : "") + ")" : "";
      var why = " " + (e.rent_why || "No rent survey covers this town.") + (e.rent_note ? " Local figures on the card run higher." : "");
      if (rent > b.rent && (!rr || rr[0] > b.rent))
        out.rent = { s: "fail", t: "Estimated 2-bed about " + money(rent) + rng + " is over " + money(b.rent) + "." + why };
      else
        out.rent = { s: "unverified", t: "Estimated 2-bed about " + money(rent) + rng +
          (rent > b.rent ? ": over your " + money(b.rent) + ", though the range reaches below it" : top > b.rent ? ": could exceed your " + money(b.rent) : ": estimate under your " + money(b.rent)) + "." + why };
    }
    else if (rent <= b.rent) out.rent = { s: "pass", t: "2-bed avg " + money(rent) + " within " + money(b.rent) };
    else if (typeof e.rent_1bed === "number" && e.rent_1bed <= b.rent)
      out.rent = { s: "pass", t: "1-bed avg " + money(e.rent_1bed) + " within budget (2-bed " + money(rent) + " is not)" };
    else out.rent = { s: "fail", t: "Avg rent " + money(rent) + " is over " + money(b.rent) };
    if (e.rent_grade && /poor/.test(e.rent_grade) && out.rent.s === "pass") out.rent.t += " (CMHC: poor reliability)";

    var r = e.rural || {}, lb = b.land || 300000;
    if (typeof r.house === "number") {
      var estd = r.house_est_pct >= 25;
      var rough = r.house_props < 100;
      var t = "Typical rural house around town " + (estd || rough ? "about " : "") + money(r.house) + (estd ? ", partly estimated" : rough ? ", a rough figure from few properties" : " assessed") +
        " (" + (r.house <= lb ? "under" : "over") + " your " + money(lb) + ")";
      if (estd) t += "; " + r.house_est_pct + "% of it rests on census estimates for " + (r.house_est_areas || []).join(", ") +
        " (no StatCan figure)" + (typeof r.house_no_est === "number" ? ", " + money(r.house_no_est) + " without them" : "");
      if (r.cheapest) t += "; cheapest nearby area " + r.cheapest[1] + " " + money(r.cheapest[0]) + (r.house_est_pct ? " (not counting estimates)" : "");
      out.land = { s: "unverified", t: t + ". Assessed values, not listings" };
    } else if (typeof r.land === "number") {
      out.land = { s: "unverified", t: "No rural house figure nearby; typical vacant lot " + money(r.land) + " assessed (" + (r.land <= lb ? "under" : "over") + " your " + money(lb) + ")" };
    } else if (r.none_within) out.land = { s: "unverified", t: "No rural area within an hour by road (scored as worst)" + (r.nearest_beyond ? "; nearest is " + r.nearest_beyond[0] + ", about " + r.nearest_beyond[1] + " min" : "") };
    else out.land = { s: "unverified", t: "No rural property figure for this town" + (r.note ? ". " + r.note : "") };

    if (!e.far_from_vancouver) out.airport = { s: "na", t: "Within ~8 h of Vancouver: airport not needed" };
    else if (typeof e.airport_h !== "number") out.airport = { s: "unknown", t: "Airport access unknown" };
    else if (e.airport_h <= b.airportHours) out.airport = { s: "pass", t: e.airport + ", about " + e.airport_h + " h" };
    else out.airport = { s: "fail", t: e.airport + " is about " + e.airport_h + " h away" };
    return out;
  }

  function isEstimate(p) {
    return !!((p.extra && p.extra.rent_estimate) || /^ESTIMATE/.test((p.prov && p.prov.rent) || ""));
  }

  function fails(p) {
    var c = checks(p);
    return c.rent.s === "fail" || c.airport.s === "fail";
  }

  // ---- scoring ----------------------------------------------------------------

  // An estimated rent is ranked at the top of its middle range (it is an estimate, and local figures run higher)
  function rankValue(p, key) {
    if (key === "rent" && isEstimate(p) && p.extra && p.extra.rent_range) return p.extra.rent_range[1];
    return p.values[key];
  }

  function percentileRanks(places, key, better) {
    var vals = places.map(function (p) { return { id: p.id, v: rankValue(p, key) }; })
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
        var r = ranks[c.key][p.id], none = false;
        if (r === undefined && /^rural_/.test(c.key) && p.extra && p.extra.rural && p.extra.rural.none_within) { r = 0; none = true; }
        if (r === undefined) { r = MISSING_RANK; wmissing += w; missing.push(c.label); }
        sum += w * r;
        parts.push({ key: c.key, label: c.label, rank: r, weight: w, none: none, missing: !none && ranks[c.key][p.id] === undefined });
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

  function computeCuts() {
    var v = state.data.places.filter(function (p) { return !fails(p) && state.scores[p.id].score !== null; })
      .map(function (p) { return state.scores[p.id].score; }).sort(function (a, b) { return a - b; });
    state.cuts = [0.2, 0.4, 0.6, 0.8].map(function (q) { return v.length ? v[Math.floor(q * (v.length - 1))] : 0; });
    state.scoreRange = v.length ? [v[0], v[v.length - 1]] : [0, 0];
  }

  function tierFor(s) {
    if (s === null) return -1;
    var i = 0;
    while (i < state.cuts.length && s > state.cuts[i]) i++;
    return i;
  }

  function colorFor(s) {
    var t = tierFor(s);
    return t < 0 ? FAIL_COLOR : TIERS[t].color;
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
    var pct = function (x) { return Math.round(x * 100); };
    var bounds = [state.scoreRange[0]].concat(state.cuts).concat([state.scoreRange[1]]);
    var rows = TIERS.slice().reverse().map(function (t, ri) {
      var i = TIERS.length - 1 - ri;
      return '<div class="lrow"><span class="sw" style="background:' + t.color + '"></span>' + t.label +
        ' <span class="muted">' + pct(bounds[i]) + "–" + pct(bounds[i + 1]) + "</span></div>";
    }).join("");
    $("legend").innerHTML = "<strong>Match (among towns that pass)</strong>" + rows +
      '<div class="lrow"><span class="sw" style="background:' + FAIL_COLOR + '"></span>Fails a budget check</div>' +
      '<div class="small muted" style="margin-top:3px">' + ($("sizeBy").value === "pop" ? "Bigger dot = more people" : "Bigger dot = better match") + ' · dashed = under 10k</div>' +
      '<label class="small sizeBy">Dot size: <select id="sizeByMirror"><option value="score">match score</option><option value="pop">population</option></select></label>';
    var mir = $("sizeByMirror");
    mir.value = $("sizeBy").value;
    mir.onchange = function () { $("sizeBy").value = mir.value; localStorage.setItem("atlas-size-by", mir.value); render(); };
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
    if (meta.ab_rent && meta.ab_rent.reference_period) periods.push("Alberta small-town rents " + meta.ab_rent.reference_period);
    if (meta.chsp) periods.push("rural property assessments " + String(meta.chsp.reference_period).split(" ")[0]);
    if (meta.business_counts) periods.push("business counts " + meta.business_counts.reference_period);
    periods.push("census May 2021 (income 2020)");
    lines.push("Data periods: " + periods.join(", ") + ". These sources publish once or twice a year.");
    el.className = "status" + (warn ? " warn" : "");
    el.innerHTML = lines.join(" ");
  }

  // ---- UI: map, rankings, detail ------------------------------------------------

  function radiusFor(p, s) {
    if ($("sizeBy").value === "pop") return Math.max(4, Math.min(16, 1.6 * Math.sqrt(p.pop2021 / 1000)));
    if (fails(p) || s.score === null) return 4;
    // size by match: weakest passing town 5 px, best 18 px
    var lo = state.scoreRange[0], hi = state.scoreRange[1];
    var t = hi > lo ? (s.score - lo) / (hi - lo) : 1;
    return 5 + 13 * t;
  }

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
    var el0 = $("detail"), sameTown = state.selected === p.id && !el0.hidden;
    var ruralOpen = sameTown && el0.querySelector("details.rural") && el0.querySelector("details.rural").open;
    var keepScroll = sameTown ? el0.scrollTop : 0;
    state.selected = p.id;
    var s = state.scores[p.id], e = p.extra || {}, ch = checks(p), rr = state.rankRange[p.id] || {};
    var crit = state.data.criteria;
    var rows = crit.map(function (c) {
      var part = s.parts.filter(function (x) { return x.key === c.key; })[0];
      var pct = !state.weights[c.key] ? "off" : part && part.none ? "worst" : part && part.missing ? "assumed 25th" : part ? ordinal(Math.round(part.rank * 100)) + " pct" : "";
      var src = p.prov && p.prov[c.key] ? '<div class="src">' + esc(p.prov[c.key]) +
        (c.key === "rent" && rankValue(p, "rent") !== p.values.rent ? "; ranked as " + money(rankValue(p, "rent")) + ", the top of its middle range" : "") + "</div>" : "";
      var none = /^rural_/.test(c.key) && p.values[c.key] === undefined && e.rural && e.rural.none_within;
      return '<tr class="' + (state.weights[c.key] ? "" : "off") + '"><td>' + esc(c.label) + src + '</td><td class="num">' + (none ? "none within 1 h" : fmt(p.values[c.key], c)) +
        (c.key === "rent" && isEstimate(p) ? ' <span class="warnText">est.</span>' : "") +
        '</td><td class="num muted">' + pct + "</td></tr>";
    }).join("");
    var lines = [];
    if (e.drive_vancouver_h !== undefined) lines.push("About " + e.drive_vancouver_h + " h drive from Vancouver (estimate)");
    if (e.airport) lines.push("Nearest airport: " + esc(e.airport) + ", about " + e.airport_h + " h" + (e.airport_note ? " (" + esc(e.airport_note) + ")" : ""));
    if (typeof p.values.rent === "number") {
      var rentSrc = (p.prov && p.prov.rent) || "";
      if (isEstimate(p)) {
        lines.push("<strong>Rent: estimate only.</strong> " + esc(e.rent_why || "No rent survey covers this town.") + " " +
          (e.rent_census_2021 ? "2021 census median renter cost " + money(e.rent_census_2021) + " a month, scaled to a 2-bed today the way it compares in other BC towns: about " + money(p.values.rent) : "Estimated 2-bed about " + money(p.values.rent)) +
          (e.rent_range ? " (scaled like the middle half of those towns: " + money(e.rent_range[0]) + " to " + money(e.rent_range[1]) +
            (e.rent_range_wide ? "; like any of them: " + money(e.rent_range_wide[0]) + " to " + money(e.rent_range_wide[1]) : "") + ")" : "") +
          ". Asking rents for newcomers are usually higher.");
      } else {
        lines.push((/Alberta/.test(rentSrc) ? "Alberta rent survey" : "CMHC") + " average rent: " + money(e.rent_1bed) + " 1-bed, " + money(p.values.rent) + " 2-bed" +
          (e.rent_source && e.rent_source !== p.name ? " (" + esc(e.rent_source) + ")" : "") +
          (e.rent_grade ? ", reliability " + esc(e.rent_grade) : "") +
          (typeof p.values.vacancy === "number" ? ", vacancy " + p.values.vacancy + "%" : "") +
          (e.rent_small_sample ? " (fewer than 30 units surveyed)" : "") +
          " [" + esc(rentSrc) + "]" + (e.rent_stale ? " (CMHC left this town out of its latest release: last known value)" : "") +
          (e.rent_kept ? " (CMHC couldn't be reached on the last refresh: last known value)" : "") +
          ". Asking rents for newcomers are usually higher.");
      }
      if (e.rent_neighbour) lines.push("CMHC's nearest centre (" + esc(e.rent_neighbour[0]) + ") averages " + money(e.rent_neighbour[1]) + " for a 2-bed; this town's own Alberta survey is used instead.");
    } else lines.push("Rent: no rent survey covers this town");
    if (e.rent_note) lines.push("Rent cross-check: " + esc(e.rent_note));
    var r = e.rural || {};
    var ruralExtras = (r.ferry_only && r.ferry_only.length ? " Left out because they need a ferry: " + r.ferry_only.map(esc).join(", ") + "." : "") +
      (r.no_data && r.no_data.length ? " No StatCan figure for: " + r.no_data.map(esc).join(", ") + "." : "") +
      (r.too_few && r.too_few.length ? " Too few properties for a reliable median (under 50): " + r.too_few.map(esc).join(", ") + "." : "") +
      (r.note ? " " + esc(r.note) : "");
    if (typeof r.house === "number" || typeof r.land === "number") {
      lines.push("Rural property around town (" + esc(r.year) + " assessed values, StatCan): typical house " + money(r.house) +
        (r.house_range && r.house_range[0] !== r.house_range[1] ? " (areas range " + money(r.house_range[0]) + " to " + money(r.house_range[1]) + ")" : "") +
        ", typical vacant lot " + money(r.land) + ". From " + (r.areas || []).filter(function (a) { return !(a[5] && a[5].unused); }).length +
        " rural area(s) with at least 10% of their homes within an hour by road from the town centre, weighted by how many" +
        ((r.areas || []).some(function (a) { return a[5] && a[5].f; }) ? "; house medians of areas only partly within the hour are adjusted to that part where census values allow" : "") + "." +
        (r.house_est_pct ? " " + r.house_est_pct + "% of the house figure rests on census estimates for areas with no StatCan figure (marked est. in the table)" +
          (typeof r.house_no_est === "number" ? "; without them it would be " + money(r.house_no_est) : "") + "." : "") + ruralExtras);
    } else {
      lines.push((r.none_within ? "Rural property: no rural area within an hour by road, so this factor counts as the worst" : "Rural property: no figure") +
        (r.nearest_beyond ? "; nearest by road is " + esc(r.nearest_beyond[0]) + " (about " + r.nearest_beyond[1] + " min, typical house " + money(r.nearest_beyond[2]) + ")" : "") + "." + ruralExtras);
    }
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
      "<table>" + rows + "</table>" + ruralTable(r);
    el.hidden = false;
    el.scrollTop = keepScroll;
    if (ruralOpen && el.querySelector("details.rural")) el.querySelector("details.rural").open = true;
    el.querySelector(".close").onclick = function () { el.hidden = true; state.selected = null; };
  }

  function ruralTable(r) {
    if (!r.areas || !r.areas.length) return "";
    function house(a) {
      var info = a[5] || {};
      if (!a[3][0]) return "n/a";
      var tip = info.est ? "No StatCan figure: 2021 census home value " + money(info.census) + " x " + info.ratio + " (assessed/census ratio of the province's rural areas)"
        : info.f ? "Whole-area median " + money(info.raw) + ", x" + info.f + " for the part within the hour (2021 census home values)" +
          (info.cap ? "; capped: census values put that part even further from the whole-area figure" : "")
        : info.unused ? "Median for the whole area. Under half of it is within the hour and census values can't tell what that part is worth, so it isn't used"
        : info.whole ? "Median for the whole area: too few census values for the part within the hour to adjust it" : "";
      return '<span title="' + esc(tip) + '">' + money(a[3][0]) + (info.est ? ' <span class="warnText">est.</span>' : info.f ? ' <span class="muted">adj.</span>' :
        info.unused ? ' <span class="warnText">not used</span>' : info.whole ? ' <span class="muted">whole</span>' : "") + "</span>" +
        (info.est ? "" : ' <span class="muted cnt">(' + a[3][1].toLocaleString() + ")</span>");
    }
    return '<details class="rural"><summary>Rural areas around town (' + r.areas.length + ")</summary>" +
      '<div class="tablewrap"><table><tr><th>Area</th><th class="num">Nearest homes</th><th class="num">Homes in 1 h</th><th class="num">Typical house</th><th class="num">Vacant lot</th></tr>' +
      r.areas.map(function (a) {
        return "<tr" + (a[5] && a[5].unused ? ' class="unusedRow"' : "") + "><td>" + esc(a[0]) + '</td><td class="num">' + a[1] + " min</td>" + '<td class="num">' + a[2] + "%</td>" +
          '<td class="num">' + house(a) + "</td>" +
          '<td class="num">' + (a[4][0] ? money(a[4][0]) + ' <span class="muted cnt">(' + a[4][1].toLocaleString() + ")</span>" : "n/a") + "</td></tr>";
      }).join("") + "</table></div>" +
      '<p class="small muted">Median ' + esc(r.year || "") + ' assessed value (number of properties). "Nearest homes" is the drive from the town centre to the area\'s closest homes; ' +
      '"Homes in 1 h" is the share of its homes you can reach within an hour by road (in BC, without a ferry). ' +
      "adj.: the area is only partly within the hour, so its house median is scaled by how 2021 census home values there compare with the whole area. " +
      "whole: only partly within the hour, but too few census values there to adjust, so the figure is for the whole area. " +
      "not used: the same, with under half the area within the hour, so it's left out of the town's figures. " +
      "est.: no StatCan figure, estimated from 2021 census home values. Vacant-lot medians are for the whole area and include tiny and remote parcels. " +
      "Assessed values usually trail sale prices.</p></details>";
  }

  function render() {
    state.scores = computeScores();
    state.rankRange = computeRankRange();
    computeCuts();
    buildLegend();
    updateGroupPct();
    var places = state.data.places.filter(visible);
    state.layer.clearLayers();

    places.slice().sort(function (a, b) {
      return radiusFor(b, state.scores[b.id]) - radiusFor(a, state.scores[a.id]); // small dots drawn on top
    }).forEach(function (p) {
      var s = state.scores[p.id], bad = fails(p);
      var m = L.circleMarker([p.lat, p.lon], {
        radius: radiusFor(p, s), color: "#2b2b2a", weight: 1,
        dashArray: p.pop2021 < 10000 ? "3 3" : null,
        fillColor: bad ? FAIL_COLOR : colorFor(s.score), fillOpacity: p.pop2021 < 10000 ? 0.8 : 0.95
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
      li.innerHTML = '<div class="r1"><span class="sw" style="background:' + colorFor(s.score) + '"></span>' + (p.pick ? "★ " : "") + esc(p.name) + ' <span class="small muted">' + (p.province === "Alberta" ? "AB" : "BC") +
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
    if (saved && saved.rural_u300 !== undefined) {  // the old census land proxy became the rural-house factor
      if (saved.rural_house === undefined) saved.rural_house = saved.rural_u300;
      delete saved.rural_u300;
      localStorage.setItem(STORE_KEY, JSON.stringify(saved));
    }
    state.weights = Object.assign(defaultWeights(), saved || {});
    state.budgets = Object.assign({}, data.budgets, savedB || {});
    state.aiMode = localStorage.getItem(MODE_KEY) === "fast" ? "fast" : "gradual";
    setAiValues(state.aiMode);

    $("budgetRent").value = state.budgets.rent;
    $("budgetAir").value = state.budgets.airportHours;
    $("budgetLand").value = state.budgets.land || 300000;
    [["budgetRent", "rent"], ["budgetAir", "airportHours"], ["budgetLand", "land"]].forEach(function (x) {
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
    buildSearch();
    $("sizeBy").value = localStorage.getItem("atlas-size-by") === "pop" ? "pop" : "score";
    $("sizeBy").addEventListener("change", function (e) { localStorage.setItem("atlas-size-by", e.target.value); render(); });
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
