const API = "https://india-air-quality.onrender.com/api";

// ── AQI helpers ────────────────────────────────────────────────────
const AQI_BANDS = [
  { max: 30,  color: "#00c853", label: "Good" },
  { max: 60,  color: "#aeea00", label: "Satisfactory" },
  { max: 90,  color: "#ffd600", label: "Moderate" },
  { max: 120, color: "#ff6d00", label: "Poor" },
  { max: 250, color: "#dd2c00", label: "Very Poor" },
  { max: Infinity, color: "#aa00ff", label: "Severe" },
];

function aqiBand(value) {
  return AQI_BANDS.find(b => value <= b.max) ?? AQI_BANDS.at(-1);
}

function pollutantColor(pollutant, value) {
  const limits = { pm10: 100, no2: 80, so2: 80, co: 4000, o3: 180 };
  if (pollutant === "pm25") return aqiBand(value).color;
  const limit = limits[pollutant] ?? 100;
  const r = value / limit;
  if (r < 0.5)  return "#00c853";
  if (r < 1.0)  return "#ffd600";
  if (r < 1.5)  return "#ff6d00";
  return "#dd2c00";
}

// Chart.js global defaults
Chart.defaults.color = "#9aa0c0";
Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";

// ── Map ──────────────────────────────────────────────────────────────
const map = L.map("map", {
  center: [20.5, 78.9],
  zoom: 5,
  zoomControl: false,        // add manually so we can place it on the left
  zoomSnap: 0.25,
  zoomDelta: 0.5,
  wheelPxPerZoomLevel: 150,
});

L.control.zoom({ position: "topleft" }).addTo(map);

L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
  attribution: "© OpenStreetMap © CARTO", subdomains: "abcd", maxZoom: 19,
}).addTo(map);

// ── State ────────────────────────────────────────────────────────────
let allStations      = [];
let markerMap        = {};
let activeStationId  = null;
let trendChart       = null;
let cityChart        = null;
let coverageChart    = null;
let compareChart     = null;
let currentPollutant = "pm25";
let topPollutedData  = [];

// ── Default marker style ─────────────────────────────────────────────
const DEFAULT_STYLE = { radius: 5, fillColor: "#5c7cfa", color: "#7b96ff", weight: 1, fillOpacity: 0.65 };
const ACTIVE_STYLE  = { radius: 7, fillColor: "#5c7cfa", color: "#ffffff", weight: 2.5, fillOpacity: 0.9 };

// ── Load all stations ────────────────────────────────────────────────
async function loadStations() {
  const res  = await fetch(`${API}/stations`);
  const data = await res.json();
  allStations = data.stations;

  for (const s of allStations) {
    const marker = L.circleMarker([s.lat, s.lng], { ...DEFAULT_STYLE });

    marker.bindPopup(`
      <div class="popup-name">${s.name}</div>
      <div class="popup-city">${s.city}</div>
      <div class="popup-tags">${s.pollutants.map(p => `<span class="popup-tag">${p}</span>`).join("")}</div>
    `);

    marker.on("click", () => openStation(s.id));
    markerMap[s.id] = marker;
    marker.addTo(map);
  }

  await loadTopPolluted();
  renderCoverageChart();
  populateCompareSelects();
  updateTimestamp();
}

// ── Top polluted ─────────────────────────────────────────────────────
async function loadTopPolluted() {
  const listEl = document.getElementById("top-polluted-list");
  listEl.innerHTML = '<div class="loading-msg">Loading…</div>';

  const res  = await fetch(`${API}/top-polluted?pollutant=${currentPollutant}&limit=15`);
  const data = await res.json();
  topPollutedData = data.ranked;

  // Color active stations on map
  for (const item of topPollutedData) {
    const m = markerMap[item.station_id];
    if (!m) continue;
    const col = pollutantColor(currentPollutant, item.value);
    m.setStyle({ fillColor: col, color: col, radius: 9, fillOpacity: 0.9, weight: 1.5 });
    m.bringToFront();
  }

  // Render list
  if (!topPollutedData.length) {
    listEl.innerHTML = '<div class="loading-msg">No data available.</div>';
    return;
  }
  listEl.innerHTML = topPollutedData.map((item, i) => {
    const col = pollutantColor(currentPollutant, item.value);
    return `<div class="polluted-item" data-id="${item.station_id}">
      <div class="polluted-rank">${i + 1}</div>
      <div class="polluted-info">
        <div class="polluted-name" title="${item.station_name}">${item.station_name}</div>
        <div class="polluted-city">${item.city}</div>
      </div>
      <div class="polluted-value" style="color:${col}">${item.value} <span style="font-size:10px;font-weight:400">µg/m³</span></div>
    </div>`;
  }).join("");

  listEl.querySelectorAll(".polluted-item").forEach(el =>
    el.addEventListener("click", () => openStation(parseInt(el.dataset.id)))
  );

  renderSnapshotCards();
  renderCityChart();
}

// ── Snapshot cards ───────────────────────────────────────────────────
function renderSnapshotCards() {
  const total     = allStations.length;
  const cities    = new Set(allStations.map(s => s.city)).size;
  const values    = topPollutedData.map(d => d.value);
  const avg       = values.length ? Math.round(values.reduce((a, b) => a + b, 0) / values.length) : "—";
  const exceed    = values.filter(v => v > 60).length;
  const exceedPct = values.length ? Math.round((exceed / values.length) * 100) + "%" : "—";

  document.getElementById("snap-total").textContent     = total.toLocaleString();
  document.getElementById("snap-stations").textContent  = total.toLocaleString();
  document.getElementById("snap-cities").textContent    = cities.toLocaleString();
  document.getElementById("snap-avg-pm25").textContent  = avg;
  document.getElementById("snap-exceed").textContent    = exceedPct;
}

// ── City PM2.5 chart (horizontal bar) ───────────────────────────────
function renderCityChart() {
  if (!topPollutedData.length) return;

  const labels = topPollutedData.map(d => {
    const n = d.station_name.length > 28 ? d.station_name.slice(0, 26) + "…" : d.station_name;
    return n;
  });
  const values = topPollutedData.map(d => d.value);
  const colors = values.map(v => pollutantColor(currentPollutant, v));

  if (cityChart) cityChart.destroy();

  const ctx = document.getElementById("city-chart").getContext("2d");
  cityChart = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [{
        data: values,
        backgroundColor: colors,
        borderRadius: 4,
        borderSkipped: false,
      }],
    },
    options: {
      indexAxis: "y",
      responsive: true,
      maintainAspectRatio: false,
      onClick: (e, elements) => {
        if (!elements.length) return;
        const idx = elements[0].index;
        const station = topPollutedData[idx];
        if (station) openStation(station.station_id);
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: ctx => ` ${ctx.parsed.x} µg/m³`,
            afterLabel: ctx => {
              const band = aqiBand(ctx.parsed.x);
              return ` ${band.label}`;
            }
          }
        }
      },
      scales: {
        x: {
          grid: { color: "#2e3147" },
          ticks: { font: { size: 11 } },
          title: { display: true, text: `${currentPollutant.toUpperCase()} µg/m³`, font: { size: 11 } },
        },
        y: {
          grid: { display: false },
          ticks: { font: { size: 11 } },
        },
      },
    },
  });
}

// ── Pollutant coverage chart (horizontal bar) ────────────────────────
function renderCoverageChart() {
  if (!allStations.length) return;

  const pollutants  = ["pm25", "pm10", "no2", "so2", "co", "o3"];
  const counts      = pollutants.map(p => allStations.filter(s => s.pollutants.includes(p)).length);
  const barColors   = ["#6c8ef5", "#5cbfef", "#5ce8c0", "#f5a623", "#f55f5f", "#b57ef5"];

  if (coverageChart) coverageChart.destroy();

  const ctx = document.getElementById("coverage-chart").getContext("2d");
  coverageChart = new Chart(ctx, {
    type: "bar",
    data: {
      labels: ["PM2.5", "PM10", "NO₂", "SO₂", "CO", "O₃"],
      datasets: [{
        data: counts,
        backgroundColor: barColors,
        borderRadius: 5,
        borderSkipped: false,
      }],
    },
    options: {
      indexAxis: "y",
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: ctx => ` ${ctx.parsed.x} stations` } }
      },
      scales: {
        x: {
          grid: { color: "#2e3147" },
          ticks: { font: { size: 11 } },
          title: { display: true, text: "Number of stations", font: { size: 11 } },
        },
        y: { grid: { display: false }, ticks: { font: { size: 12 } } },
      },
    },
  });
}

// ── Compare selects ──────────────────────────────────────────────────
function populateCompareSelects() {
  const pm25Stations = allStations
    .filter(s => s.pollutants.includes("pm25"))
    .sort((a, b) => a.name.localeCompare(b.name));

  const options = pm25Stations.map(s =>
    `<option value="${s.id}">${s.name} (${s.city})</option>`
  ).join("");

  document.querySelectorAll(".compare-select").forEach(sel => {
    const first = sel.options[0].outerHTML;
    sel.innerHTML = first + options;
  });

  // Pre-select a few interesting defaults
  const selects = document.querySelectorAll(".compare-select");
  const presets = pm25Stations.filter(s =>
    s.name.toLowerCase().includes("new delhi") ||
    s.name.toLowerCase().includes("hyderabad") ||
    s.name.toLowerCase().includes("bengaluru")
  );
  presets.slice(0, 3).forEach((s, i) => {
    if (selects[i]) selects[i].value = s.id;
  });
}

// ── Run compare ──────────────────────────────────────────────────────
document.getElementById("run-compare-btn").addEventListener("click", async () => {
  const ids = [...document.querySelectorAll(".compare-select")]
    .map(s => s.value)
    .filter(v => v);

  if (!ids.length) return;

  const emptyEl   = document.getElementById("compare-empty");
  const loadingEl = document.getElementById("compare-loading");
  emptyEl.classList.add("hidden");
  loadingEl.classList.remove("hidden");

  try {
    const res  = await fetch(`${API}/compare?ids=${ids.join(",")}&pollutant=pm25&days=30`);
    const data = await res.json();

    loadingEl.classList.add("hidden");

    if (!data.stations?.length) {
      emptyEl.textContent = "No trend data found for selected stations.";
      emptyEl.classList.remove("hidden");
      return;
    }

    const COLORS = ["#6c8ef5", "#00c853", "#ffd600", "#ff6d00", "#aa00ff"];

    // Build unified date axis
    const allDates = [...new Set(
      data.stations.flatMap(s => s.series.map(d => d.date))
    )].sort();

    const datasets = data.stations.map((s, i) => {
      const byDate = Object.fromEntries(s.series.map(d => [d.date, d.value]));
      return {
        label: s.city,
        data:  allDates.map(d => byDate[d] ?? null),
        borderColor:     COLORS[i % COLORS.length],
        backgroundColor: COLORS[i % COLORS.length] + "22",
        tension:         0.3,
        fill:            true,
        pointRadius:     2,
        spanGaps:        true,
      };
    });

    if (compareChart) compareChart.destroy();

    const ctx = document.getElementById("compare-chart").getContext("2d");
    compareChart = new Chart(ctx, {
      type: "line",
      data: { labels: allDates.map(d => d.slice(5)), datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { position: "top", labels: { boxWidth: 12, font: { size: 11 } } },
          tooltip: { callbacks: { label: ctx => ` ${ctx.dataset.label}: ${ctx.parsed.y ?? "N/A"} µg/m³` } }
        },
        scales: {
          x: { grid: { color: "#2e3147" }, ticks: { font: { size: 10 }, maxTicksLimit: 15 } },
          y: {
            grid: { color: "#2e3147" },
            ticks: { font: { size: 11 } },
            title: { display: true, text: "PM2.5 µg/m³", font: { size: 11 } },
            beginAtZero: true,
          },
        },
      },
    });
  } catch (e) {
    loadingEl.classList.add("hidden");
    emptyEl.textContent = "Failed to load trend data.";
    emptyEl.classList.remove("hidden");
  }
});

// ── Open station detail ──────────────────────────────────────────────
async function openStation(stationId) {
  activeStationId = stationId;
  const station   = allStations.find(s => s.id === stationId);
  if (!station) return;

  // Highlight marker
  Object.values(markerMap).forEach(m => {
    if (m._wasHighlighted) { m.setStyle({ weight: 1 }); }
  });
  const active = markerMap[stationId];
  if (active) { active.setStyle({ weight: 3, color: "#ffffff" }); active._wasHighlighted = true; }

  document.getElementById("panel-station-name").textContent = station.name;
  document.getElementById("panel-city").textContent = `${station.city} · ${station.provider}`;
  document.getElementById("readings-grid").innerHTML = "";
  document.getElementById("aqi-badge").textContent   = "—";
  document.getElementById("aqi-label").textContent   = "Loading…";
  document.getElementById("chart-empty").classList.add("hidden");
  document.getElementById("chart-loading").classList.remove("hidden");
  document.getElementById("right-panel").classList.remove("hidden");
  showBackdrop();

  // Scroll map into view if we're in analytics section
  document.querySelector(".map-section").scrollIntoView({ behavior: "smooth" });

  try {
    const res  = await fetch(`${API}/stations/${stationId}/latest`);
    const data = await res.json();
    renderReadings(data.readings);
  } catch {
    document.getElementById("readings-grid").innerHTML =
      '<div style="padding:12px;color:#9aa0c0;font-size:12px;grid-column:1/-1">Could not load readings.</div>';
  }

  await renderTrend(stationId);
}

function renderReadings(readings) {
  const grid = document.getElementById("readings-grid");
  grid.innerHTML = "";

  if (!readings || !Object.keys(readings).length) {
    grid.innerHTML = '<div style="padding:12px;color:#9aa0c0;font-size:12px;grid-column:1/-1">No recent readings.</div>';
    return;
  }

  const pm25 = readings["pm25"];
  if (pm25) {
    const band = aqiBand(pm25.value);
    document.getElementById("aqi-badge").textContent    = pm25.value;
    document.getElementById("aqi-badge").style.color    = band.color;
    document.getElementById("aqi-label").innerHTML =
      `<span style="color:${band.color};font-weight:700">${band.label}</span><br>PM2.5 · µg/m³<br>
       <span style="font-size:10px">${pm25.timestamp ? pm25.timestamp.slice(0,16).replace("T"," ") : ""}</span>`;
  }

  const ORDER = ["pm25", "pm10", "no2", "so2", "co", "o3"];
  const keys  = ORDER.filter(k => readings[k]).concat(
    Object.keys(readings).filter(k => !ORDER.includes(k))
  );

  for (const key of keys) {
    const r   = readings[key];
    const col = pollutantColor(key, r.value);
    const ts  = r.timestamp ? r.timestamp.slice(0, 16).replace("T", " ") : "";
    grid.innerHTML += `
      <div class="reading-card">
        <div class="reading-label">${key.toUpperCase()}</div>
        <div class="reading-value" style="color:${col}">${r.value}</div>
        <div class="reading-unit">${r.units}</div>
        <div class="reading-ts">${ts}</div>
      </div>`;
  }
}

async function renderTrend(stationId) {
  const days = document.getElementById("trend-days").value;
  document.getElementById("chart-loading").classList.remove("hidden");
  document.getElementById("chart-empty").classList.add("hidden");
  document.getElementById("chart-title").textContent = `${currentPollutant.toUpperCase()} — ${days}-day trend`;

  try {
    const res  = await fetch(`${API}/stations/${stationId}/trend?pollutant=${currentPollutant}&days=${days}`);
    const data = await res.json();
    document.getElementById("chart-loading").classList.add("hidden");

    if (!data.series?.length) {
      document.getElementById("chart-empty").classList.remove("hidden");
      return;
    }

    const labels = data.series.map(d => d.date.slice(5));
    const values = data.series.map(d => d.value);
    const colors = values.map(v => pollutantColor(currentPollutant, v));

    if (trendChart) trendChart.destroy();
    const ctx = document.getElementById("trend-chart").getContext("2d");
    trendChart = new Chart(ctx, {
      type: "bar",
      data: {
        labels,
        datasets: [{ data: values, backgroundColor: colors, borderRadius: 3, borderSkipped: false }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: ctx => ` ${ctx.parsed.y} µg/m³` } }
        },
        scales: {
          x: { ticks: { color: "#9aa0c0", font: { size: 9 }, maxRotation: 0 }, grid: { color: "#2e3147" } },
          y: { ticks: { color: "#9aa0c0", font: { size: 10 } }, grid: { color: "#2e3147" }, beginAtZero: true },
        },
      },
    });
  } catch {
    document.getElementById("chart-loading").classList.add("hidden");
    document.getElementById("chart-empty").classList.remove("hidden");
  }
}

// ── Search ────────────────────────────────────────────────────────────
document.getElementById("search-input").addEventListener("input", async function () {
  const q = this.value.trim().toLowerCase();
  if (!q) {
    Object.values(markerMap).forEach(m => m.setStyle({ ...DEFAULT_STYLE }));
    await loadTopPolluted();
    return;
  }

  Object.values(markerMap).forEach(m =>
    m.setStyle({ fillColor: "#2e3147", color: "#3a3f5c", radius: 3, fillOpacity: 0.3 })
  );

  const matches = allStations.filter(
    s => s.name.toLowerCase().includes(q) || s.city.toLowerCase().includes(q)
  );

  for (const s of matches) {
    markerMap[s.id]?.setStyle({ fillColor: "#6c8ef5", color: "#ffffff", radius: 9, fillOpacity: 0.95, weight: 2 });
    markerMap[s.id]?.bringToFront();
  }

  if (matches.length === 1) map.setView([matches[0].lat, matches[0].lng], 11);
  else if (matches.length > 1) {
    map.fitBounds(L.latLngBounds(matches.map(s => [s.lat, s.lng])), { padding: [60, 60] });
  }
});

// ── Pollutant change ──────────────────────────────────────────────────
document.getElementById("pollutant-select").addEventListener("change", async function () {
  currentPollutant = this.value;
  Object.values(markerMap).forEach(m => m.setStyle({ ...DEFAULT_STYLE }));
  await loadTopPolluted();
  if (activeStationId) await renderTrend(activeStationId);
});

// ── Trend days change ─────────────────────────────────────────────────
document.getElementById("trend-days").addEventListener("change", async function () {
  if (activeStationId) await renderTrend(activeStationId);
});

// ── Panel helpers (backdrop on mobile) ────────────────────────────────
const backdrop = document.getElementById("panel-backdrop");

function isMobile() { return window.innerWidth <= 768; }

function showBackdrop() { if (isMobile()) backdrop.style.display = "block"; }
function hideBackdrop() { backdrop.style.display = "none"; }

backdrop.addEventListener("click", () => {
  document.getElementById("left-panel").classList.add("hidden");
  document.getElementById("right-panel").classList.add("hidden");
  hideBackdrop();
  activeStationId = null;
  Object.values(markerMap).forEach(m => m.setStyle({ weight: 1 }));
});

// ── Panel toggles ─────────────────────────────────────────────────────
document.getElementById("toggle-panel-btn").addEventListener("click", () => {
  const panel = document.getElementById("left-panel");
  const isHidden = panel.classList.toggle("hidden");
  isHidden ? hideBackdrop() : showBackdrop();
});
document.getElementById("close-left-panel").addEventListener("click", () => {
  document.getElementById("left-panel").classList.add("hidden");
  hideBackdrop();
});
document.getElementById("close-right-panel").addEventListener("click", () => {
  document.getElementById("right-panel").classList.add("hidden");
  hideBackdrop();
  activeStationId = null;
  Object.values(markerMap).forEach(m => m.setStyle({ weight: 1 }));
});

// ── Timestamp ─────────────────────────────────────────────────────────
function updateTimestamp() {
  const now = new Date();
  document.getElementById("last-updated").textContent =
    `· ${now.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" })}`;
}

// ── Boot ──────────────────────────────────────────────────────────────
loadStations();
