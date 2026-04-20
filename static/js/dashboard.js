(function () {
  const canvas = document.getElementById("rail-map");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const stateFilter = document.getElementById("filter-state");
  const zoneFilter = document.getElementById("filter-zone");
  const catFilter = document.getElementById("filter-category");
  const smartSearch = document.getElementById("smart-search");
  const statusEl = document.getElementById("map-status");
  let live = null;

  function drawMap() {
    if (!live) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const blink = (Date.now() / 500) % 2 < 1;
    live.routes.forEach((route) => {
      const a = live.stations.find((s) => s.name === route.from);
      const b = live.stations.find((s) => s.name === route.to);
      if (!a || !b) return;
      const occupied = live.occupied.includes(route.id);
      const conflicted = live.conflicts.includes(route.id);
      ctx.strokeStyle = conflicted && blink ? "#ff2e2e" : occupied ? "#f08a24" : "#9ab2cd";
      ctx.lineWidth = conflicted ? 4 : 2;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    });

    live.stations.forEach((s) => {
      const stateValue = stateFilter ? stateFilter.value : "";
      const zoneValue = zoneFilter ? zoneFilter.value : "";
      const stateOk = !stateValue || s.state.toLowerCase().includes(stateValue.toLowerCase());
      const zoneOk = !zoneValue || s.zone.toLowerCase().includes(zoneValue.toLowerCase());
      if (!stateOk || !zoneOk) return;
      ctx.fillStyle = "#0b4f8c";
      ctx.beginPath();
      ctx.arc(s.x, s.y, 5, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "#1c2430";
      ctx.font = "12px Segoe UI";
      ctx.fillText(s.name.replace(" Railway Station", "").replace(" Junction", ""), s.x + 8, s.y - 6);
    });

    live.trains.forEach((t) => {
      const query = smartSearch ? smartSearch.value.toLowerCase() : "";
      if (query && !t.code.toLowerCase().includes(query)) return;
      ctx.fillStyle = t.decision === "HOLD" ? "#c45c26" : "#0f8b4c";
      ctx.beginPath();
      ctx.arc(t.x, t.y, 3.5, 0, Math.PI * 2);
      ctx.fill();
    });
    statusEl.textContent = `Live update ${new Date(live.timestamp).toLocaleTimeString()} | occupied routes: ${live.occupied.length} | conflicts: ${live.conflicts.length}`;
  }

  async function fetchMap() {
    const res = await fetch("/api/live-map");
    live = await res.json();
    drawMap();
  }

  let throughputChart;
  async function fetchAnalytics() {
    const res = await fetch("/api/analytics");
    const data = await res.json();
    const common = { labels: data.labels };
    const options = { responsive: true, animation: false };
    const cfg = (label, arr, color) => ({ type: "line", data: { ...common, datasets: [{ label, data: arr, borderColor: color, backgroundColor: color }] }, options });

    if (!throughputChart) {
      throughputChart = new Chart(document.getElementById("throughput-chart"), cfg("Throughput trend", data.throughput, "#0b4f8c"));
      new Chart(document.getElementById("delay-chart"), cfg("Delay trend", data.delay, "#c45c26"));
      new Chart(document.getElementById("util-chart"), cfg("Platform utilization", data.utilization, "#0f8b4c"));
      new Chart(document.getElementById("conflict-chart"), cfg("Conflict count reduction", data.conflicts, "#aa2ee6"));
    } else {
      throughputChart.data.labels = data.labels;
      throughputChart.data.datasets[0].data = data.throughput;
      throughputChart.update();
    }
  }

  [stateFilter, zoneFilter, catFilter, smartSearch].forEach((el) => el && el.addEventListener("input", drawMap));
  fetchMap();
  fetchAnalytics();
  setInterval(fetchMap, 3000);
  setInterval(fetchAnalytics, 5000);
})();
