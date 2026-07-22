// Dashboard page logic: loads the /api/stats summary and fills the item
// cards, the per-stage bar chart (inline SVG) and the LLM block.
// The only network request is same-origin /api/stats via window.OpsCenter.apiFetch.
"use strict";

(function () {
  const page = document.querySelector('[data-page="dashboard"]');
  if (!page) return;

  const errorEl = page.querySelector("[data-dashboard-error]");
  const chartEl = page.querySelector("[data-stage-chart]");

  function setText(selector, value) {
    const el = page.querySelector(selector);
    if (el) el.textContent = String(value);
  }

  function showError(message) {
    if (!errorEl) return;
    errorEl.textContent = message;
    errorEl.hidden = false;
  }

  // Horizontal chart: one row per stage -- so long stage names don't collide.
  // Each row: name on the left, item count and age on the right, a bar beneath
  // them proportional to deal_count (the largest stage = full width). Inline SVG
  // via createElementNS; all text via textContent, so it's XSS-safe.
  const SVG_NS = "http://www.w3.org/2000/svg";
  const CHART_W = 460;
  const ROW_H = 38;
  const BAR_H = 10;
  const PAD_X = 2;

  function svgText(cls, x, y, anchor, text) {
    const el = document.createElementNS(SVG_NS, "text");
    el.setAttribute("class", cls);
    el.setAttribute("x", String(x));
    el.setAttribute("y", String(y));
    el.setAttribute("text-anchor", anchor);
    el.textContent = text;
    return el;
  }

  function renderStageChart(stages) {
    chartEl.innerHTML = "";
    if (!stages || stages.length === 0) {
      const empty = document.createElement("p");
      empty.className = "page-hint";
      empty.textContent = "No active stages.";
      chartEl.appendChild(empty);
      return;
    }

    const maxCount = Math.max(1, ...stages.map((s) => s.deal_count));
    const totalH = stages.length * ROW_H + 6;

    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "stage-chart-svg");
    svg.setAttribute("viewBox", `0 0 ${CHART_W} ${totalH}`);
    svg.setAttribute("width", String(CHART_W));
    svg.setAttribute("height", String(totalH));

    stages.forEach((stage, i) => {
      const rowY = i * ROW_H + 6;
      const labelY = rowY + 10;
      const barY = rowY + 18;
      const trackW = CHART_W - PAD_X * 2;
      const ratio = stage.deal_count / maxCount;
      const fillW = stage.deal_count > 0 ? Math.max(4, Math.round(ratio * trackW)) : 0;

      // Stage name on the left (full row width -- doesn't collide with neighbors).
      svg.appendChild(svgText("stage-bar-label", PAD_X, labelY, "start", stage.name));

      // On the right: item count (accented) and age (muted).
      const metric = document.createElementNS(SVG_NS, "text");
      metric.setAttribute("x", String(CHART_W - PAD_X));
      metric.setAttribute("y", String(labelY));
      metric.setAttribute("text-anchor", "end");
      const age = document.createElementNS(SVG_NS, "tspan");
      age.setAttribute("class", "stage-bar-age");
      age.textContent = `  ${stage.avg_workdays_in_stage}/${stage.max_workdays_in_stage} d.`;
      const count = document.createElementNS(SVG_NS, "tspan");
      count.setAttribute("class", "stage-bar-count");
      count.textContent = String(stage.deal_count);
      metric.appendChild(count);
      metric.appendChild(age);
      svg.appendChild(metric);

      // Track and bar.
      const track = document.createElementNS(SVG_NS, "rect");
      track.setAttribute("class", "stage-bar-track");
      track.setAttribute("x", String(PAD_X));
      track.setAttribute("y", String(barY));
      track.setAttribute("width", String(trackW));
      track.setAttribute("height", String(BAR_H));
      track.setAttribute("rx", "5");
      svg.appendChild(track);

      const bar = document.createElementNS(SVG_NS, "rect");
      bar.setAttribute("class", "stage-bar");
      bar.setAttribute("x", String(PAD_X));
      bar.setAttribute("y", String(barY));
      bar.setAttribute("width", String(fillW));
      bar.setAttribute("height", String(BAR_H));
      bar.setAttribute("rx", "5");
      const title = document.createElementNS(SVG_NS, "title");
      title.textContent = `${stage.name}: ${stage.deal_count} items, `
        + `${stage.avg_workdays_in_stage} avg / ${stage.max_workdays_in_stage} max business days`;
      bar.appendChild(title);
      svg.appendChild(bar);
    });

    chartEl.appendChild(svg);
  }

  async function load() {
    let stats;
    try {
      stats = await window.OpsCenter.apiFetch("/api/stats");
    } catch (err) {
      showError(err.message || "Failed to load the statistics");
      return;
    }

    setText("[data-stat-total]", stats.deals_total);
    setText("[data-stat-active]", stats.deals_active);
    setText("[data-stat-closed]", stats.deals_closed);

    renderStageChart(stats.stages);

    const llm = stats.llm || {};
    setText("[data-llm-total]", llm.total_calls);
    setText("[data-llm-success]", llm.success_calls);
    setText("[data-llm-error]", llm.error_calls);
    setText("[data-llm-duration]", llm.avg_duration_ms);
    setText("[data-llm-input-tokens]", llm.input_tokens);
    setText("[data-llm-output-tokens]", llm.output_tokens);
  }

  load();
})();
