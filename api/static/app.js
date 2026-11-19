/* Neuro-Air Lite: Full Multi-Agent Matrix Animation & Executive KPI Stream Consumer */
(() => {
  "use strict";

  const form = document.getElementById("chat-form");
  const queryInput = document.getElementById("query");
  const charCount = document.getElementById("char-count");
  const sendButton = document.getElementById("send");
  const btnLabel = document.getElementById("btn-label");
  const statusEl = document.getElementById("status");
  const statusText = document.getElementById("status-text");
  const agentDot = document.getElementById("agent-dot");
  const agentStatusLabel = document.getElementById("agent-status-label");
  const answerEl = document.getElementById("answer");
  const answerMeta = document.getElementById("answer-meta");
  const chartsEl = document.getElementById("charts");
  const chartsPanelArea = document.getElementById("charts-panel-area");
  const chartsPanelEmpty = document.getElementById("charts-panel-empty");
  const errorEl = document.getElementById("error");
  const traceEl = document.getElementById("trace");
  const traceEmptyState = document.getElementById("trace-empty-state");
  const traceMeta = document.getElementById("trace-meta");
  const traceNavBadge = document.getElementById("trace-nav-badge");
  const chartsNavBadge = document.getElementById("charts-nav-badge");
  const bcCurrent = document.getElementById("bc-current");
  const referenceBody = document.getElementById("reference-body");
  const referenceCharts = document.getElementById("reference-charts");
  const referenceStatus = document.getElementById("reference-status");
  const referenceCaveat = document.getElementById("reference-caveat");

  // Matrix Elements
  const matrixOverlay = document.getElementById("matrix-overlay");
  const matrixTaskLabel = document.getElementById("matrix-task-label");
  const matrixSvg = document.getElementById("matrix-svg");
  const canvasWrapper = document.getElementById("canvas-wrapper");
  const hudText = document.getElementById("hud-text");
  const hudStep = document.getElementById("hud-step");

  let chartInstances = [];
  let referenceChartInstances = [];
  let running = false;
  let traceEventsCount = 0;
  let activeRole = null;
  let roleHasReturned = false;

  // Node relative canvas coordinates
  const nodeCoords = {
    coordinator: { x: 480, y: 310 },
    knowledge:   { x: 230, y: 110 },
    data:        { x: 480, y: 88 },
    meteo:       { x: 730, y: 110 },
    pollution:   { x: 730, y: 485 },
    synthesis:   { x: 480, y: 535 },
    verifier:    { x: 230, y: 485 }
  };

  // Map API role names to DOM node IDs
  const roleToNodeKey = {
    coordinator: "coordinator",
    data_acquisition: "data",
    knowledge_retrieval: "knowledge",
    multivariate_meteo: "meteo",
    pollution_analysis: "pollution",
    synthesis_report: "synthesis",
    verification: "verifier",
    verifier: "verifier"
  };

  // Tab Navigation
  document.querySelectorAll(".sidebar-nav .nav-item").forEach((link) => {
    link.addEventListener("click", (e) => {
      e.preventDefault();
      const targetPanel = link.getAttribute("data-panel");
      
      document.querySelectorAll(".sidebar-nav .nav-item").forEach(item => item.classList.remove("active"));
      link.classList.add("active");

      document.querySelectorAll(".content-area").forEach(sec => sec.hidden = true);
      const activeSec = document.getElementById(`panel-${targetPanel}`);
      if (activeSec) activeSec.hidden = false;

      const titleMap = { query: "Dashboard", trace: "Execution Trace", charts: "Visualizations" };
      if (bcCurrent) bcCurrent.textContent = titleMap[targetPanel] || "Workspace";

      if (targetPanel === "charts" || targetPanel === "query") {
        setTimeout(() => chartInstances.forEach(c => c.resize()), 80);
      }
    });
  });

  if (queryInput && charCount) {
    queryInput.addEventListener("input", () => {
      charCount.textContent = queryInput.value.length;
    });
  }

  document.querySelectorAll(".sample-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      if (running) return;
      queryInput.value = chip.getAttribute("data-q") || "";
      if (charCount) charCount.textContent = queryInput.value.length;
      queryInput.focus();
    });
  });

  function setStatus(text, mode = "") {
    if (statusText) statusText.textContent = text;
    if (statusEl) statusEl.className = `status-pill ${mode}`.trim();
    if (agentDot) agentDot.className = `status-dot ${mode}`.trim();
    if (agentStatusLabel) agentStatusLabel.textContent = text;
  }

  function initStaticGridLines() {
    if (!matrixSvg) return;
    matrixSvg.innerHTML = "";
    const c = nodeCoords.coordinator;

    for (const [key, pt] of Object.entries(nodeCoords)) {
      if (key === "coordinator") continue;
      const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      line.setAttribute("x1", c.x);
      line.setAttribute("y1", c.y);
      line.setAttribute("x2", pt.x);
      line.setAttribute("y2", pt.y);
      line.setAttribute("stroke", "rgba(56, 189, 248, 0.12)");
      line.setAttribute("stroke-width", "1.5");
      line.setAttribute("stroke-dasharray", "4 4");
      matrixSvg.appendChild(line);
    }
  }

  function animatePacket(fromKey, toKey, label, isReturn = false, durationMs = 900) {
    return new Promise(resolve => {
      const from = nodeCoords[fromKey] || nodeCoords.coordinator;
      const to = nodeCoords[toKey] || nodeCoords.coordinator;
      if (!canvasWrapper) return resolve();

      const packet = document.createElement("div");
      packet.className = "packet-badge" + (isReturn ? " ret" : "");
      packet.innerText = label;
      packet.style.left = `${from.x}px`;
      packet.style.top = `${from.y}px`;
      canvasWrapper.appendChild(packet);

      const startTime = performance.now();

      function move(now) {
        const elapsed = now - startTime;
        const progress = Math.min(elapsed / durationMs, 1);
        const ease = progress < 0.5 ? 2 * progress * progress : -1 + (4 - 2 * progress) * progress;

        const curX = from.x + (to.x - from.x) * ease;
        const curY = from.y + (to.y - from.y) * ease;

        packet.style.left = `${curX}px`;
        packet.style.top = `${curY}px`;

        if (progress < 1) {
          requestAnimationFrame(move);
        } else {
          packet.remove();
          resolve();
        }
      }
      requestAnimationFrame(move);
    });
  }

  function setActiveNode(key, active = true) {
    const el = document.getElementById(`node-${key}`);
    if (!el) return;
    if (active) el.classList.add("active");
    else el.classList.remove("active");
  }

  function clearAllActiveNodes() {
    document.querySelectorAll(".agent-node").forEach(n => n.classList.remove("active"));
  }

  function setRunning(value, isMulti = false) {
    running = value;
    if (sendButton) sendButton.disabled = value;
    if (queryInput) queryInput.disabled = value;
    document.querySelectorAll("input[name='arm']").forEach(i => i.disabled = value);

    if (btnLabel) btnLabel.textContent = value ? "Orchestrating..." : "Run Query";

    if (matrixOverlay) {
      if (value && isMulti) {
        initStaticGridLines();
        clearAllActiveNodes();
        matrixOverlay.classList.add("active");
      } else {
        matrixOverlay.classList.remove("active");
        clearAllActiveNodes();
      }
    }
  }

  function resetOutput(arm = "single") {
    traceEventsCount = 0;
    if (traceNavBadge) traceNavBadge.hidden = true;
    if (chartsNavBadge) chartsNavBadge.hidden = true;

    const safeLabel = esc(
      arm === "multi"
        ? "Executing multi-agent pipeline via read-only runtime..."
        : "Executing single-agent pipeline via read-only runtime..."
    );
    answerEl.className = "answer-body empty";
    answerEl.innerHTML = `
      <div class="empty-state">
        <svg width="38" height="38" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
        <p>${safeLabel}</p>
      </div>
    `;
    answerMeta.replaceChildren();
    chartsEl.replaceChildren();
    if (chartsPanelArea) chartsPanelArea.replaceChildren();
    if (chartsPanelEmpty) chartsPanelEmpty.hidden = false;

    traceEl.replaceChildren();
    if (traceEmptyState) traceEmptyState.hidden = false;
    if (traceMeta) traceMeta.textContent = "";

    errorEl.hidden = true;
    errorEl.textContent = "";
    disposeCharts();
  }

  function disposeCharts() {
    for (const chart of chartInstances) {
      try { chart.dispose(); } catch (_) {}
    }
    chartInstances = [];
  }

  function addTrace(kind, textContent, isCode = false) {
    traceEventsCount++;
    if (traceEmptyState) traceEmptyState.hidden = true;
    if (traceNavBadge) {
      traceNavBadge.textContent = traceEventsCount;
      traceNavBadge.hidden = false;
    }
    if (traceMeta) {
      traceMeta.textContent = `${traceEventsCount} step${traceEventsCount === 1 ? "" : "s"}`;
    }

    const item = document.createElement("li");
    item.className = "trace-item";

    const header = document.createElement("div");
    header.className = "trace-header";

    const tag = document.createElement("span");
    const tagClass = {
      plan: "tag-plan",
      worker: "tag-worker",
      python: "tag-python",
      output: "tag-output",
      verifier: "tag-plan",
      error: "tag-error"
    }[kind] || "tag-default";
    tag.className = `trace-tag ${tagClass}`;
    tag.textContent = kind;
    header.appendChild(tag);
    item.appendChild(header);

    if (isCode) {
      const details = document.createElement("details");
      details.open = true;
      const summary = document.createElement("summary");
      summary.textContent = "Python Script";
      const pre = document.createElement("pre");
      pre.className = "trace-code-block";
      pre.textContent = textContent;
      details.appendChild(summary);
      details.appendChild(pre);
      item.appendChild(details);
    } else {
      const p = document.createElement("div");
      p.style.fontSize = "0.85rem";
      p.style.color = "#c9d1d9";
      p.style.whiteSpace = "pre-wrap";
      p.textContent = textContent;
      item.appendChild(p);
    }

    traceEl.appendChild(item);
  }

  function text(value) {
    if (value === null || value === undefined) return "";
    if (typeof value === "string") return value;
    try { return JSON.stringify(value); } catch (_) { return String(value); }
  }

  function esc(value) {
    return String(value === null || value === undefined ? "" : value).replace(
      /[&<>"']/g,
      (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch])
    );
  }

  // Render Elegant Executive KPI Cards instead of raw JSON dict
  function renderExecutiveCards(answer) {
    if (!answer) return "";

    // If answer is an object/dict (like {'Central/Western': 17.43, 'Tap Mun': 9.13})
    if (typeof answer === "object" && !Array.isArray(answer)) {
      const keys = Object.keys(answer);
      if (keys.length > 0) {
        let cardsHtml = `<div class="kpi-grid">`;
        keys.forEach((key, idx) => {
          const val = answer[key];
          const isNum = typeof val === "number";
          const safeCardClass = idx % 2 === 1 ? "kpi-card secondary" : "kpi-card";
          const safeKey = esc(key);
          const safeValue = esc(isNum ? val.toLocaleString() : text(val));
          const safeTag = esc(isNum ? "Calculated value" : "Answer attribute");
          cardsHtml += `
            <div class="${safeCardClass}">
              <div class="kpi-label">${safeKey}</div>
              <div class="kpi-value-row">
                <span class="kpi-num">${safeValue}</span>
              </div>
              <span class="kpi-tag">${safeTag}</span>
            </div>
          `;
        });
        cardsHtml += `</div>`;
        return cardsHtml;
      }
    }

    // Single scalar number. No unit is invented here; only the payload may
    // carry one, and the current API payload has no unit field.
    if (typeof answer === "number") {
      const safeNumber = esc(answer.toLocaleString());
      return `
        <div class="kpi-grid">
          <div class="kpi-card">
            <div class="kpi-label">Computed Result</div>
            <div class="kpi-value-row">
              <span class="kpi-num">${safeNumber}</span>
            </div>
            <span class="kpi-tag">Calculated value</span>
          </div>
        </div>
      `;
    }

    // Default formatted tag
    const safeAnswer = esc(text(answer));
    return `<div class="kpi-tag" style="font-size:0.85rem; padding: 4px 10px; margin-bottom: 0.8rem;">ANSWER: ${safeAnswer}</div>`;
  }

  // Convert simple markdown headings and bold text into styled HTML. Escape
  // the model/DB text first so the only HTML in the result is the small set of
  // tags added by these regex replacements.
  function formatAnalysisText(rawText) {
    if (!rawText) return "<p>(No analysis generated)</p>";

    let formatted = esc(rawText)
      .replace(/^### (.*$)/gim, '<h4 class="analysis-subheading">$1</h4>')
      .replace(/^## (.*$)/gim, '<h3 class="analysis-heading">$1</h3>')
      .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
      .replace(/\*(.*?)\*/g, '<em>$1</em>')
      .replace(/^- (.*$)/gim, '<li class="analysis-bullet">$1</li>');

    // Wrap list items
    formatted = formatted.replace(/(<li.*<\/li>)/gms, '<ul class="analysis-list">$1</ul>');

    // Split paragraphs
    return formatted.split(/\n\n+/).map(p => {
      p = p.trim();
      if (!p) return "";
      if (p.startsWith("<h") || p.startsWith("<ul")) return p;
      return '<p class="analysis-p">' + p + '</p>';
    }).join("");
  }

  function verifierDisplay(data) {
    if (data.arm !== "multi") {
      return { label: "Single agent - no verifier", className: "chip-neutral", ok: false };
    }
    const status = data.verifier_status || "unknown";
    if (status === "not_run") {
      return { label: "Verifier not run", className: "chip-neutral", ok: false };
    }
    if (status === "changed") {
      return { label: "Verifier changed stored answer", className: "chip-neutral", ok: false };
    }
    if (status === "mismatch") {
      return { label: "Verifier reported mismatch", className: "chip-neutral", ok: false };
    }
    if (status === "failed") {
      return { label: "Verifier failed", className: "chip-neutral", ok: false };
    }
    if (status === "confirmed") {
      return { label: "Verifier confirmed stored answer", className: "chip-success", ok: true };
    }
    return { label: "Verifier outcome unknown", className: "chip-neutral", ok: false };
  }

  function renderVerifierBanner(data) {
    if (data.arm !== "multi") return "";
    if (data.verifier_status === "changed") {
      if (data.verifier_mode === "flag") {
        return '<div class="verifier-banner">Verifier changed the stored answer during audit; flag mode kept the synthesis answer.</div>';
      }
      const safeBefore = esc(text(data.answer_pre_verify));
      const safeAfter = esc(text(data.answer));
      const safeBannerText = "Verifier changed the stored answer: " + safeBefore + " &rarr; " + safeAfter;
      return '<div class="verifier-banner">' + safeBannerText + '</div>';
    }
    if (data.verifier_status === "mismatch") {
      return '<div class="verifier-banner">Verifier reported a mismatch. Review this result before use.</div>';
    }
    if (data.verifier_status === "failed") {
      return '<div class="verifier-banner">Verifier did not complete; the answer is unverified synthesis output.</div>';
    }
    return "";
  }

  function showAnswer(data) {
    answerEl.className = "answer-body";
    answerEl.replaceChildren();

    const verifier = verifierDisplay(data);
    const safeAnswerHtml = renderExecutiveCards(data.answer);
    const safeAnalysisHtml = formatAnalysisText(data.final_text);
    const safeChipClass = esc(verifier.className);
    const safeChipLabel = esc(verifier.label);
    const safeChipIcon = verifier.ok
      ? '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>'
      : '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="9"/><path d="M12 8v5"/><path d="M12 16h.01"/></svg>';
    const safeVerifierBanner = renderVerifierBanner(data);
    const safeDraftHtml = data.synthesis_draft
      ? '<details class="synthesis-draft"><summary>Synthesis draft (pre-verifier, not final)</summary><div class="analysis-prose">' +
        formatAnalysisText(data.synthesis_draft) +
        '</div></details>'
      : "";

    const container = document.createElement("div");
    container.innerHTML = `
      <div class="answer-header-strip">
        <span class="answer-header-title">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
          Atmospheric & Environmental Assessment
        </span>
        <span class="chip ${safeChipClass}">
          ${safeChipIcon}
          ${safeChipLabel}
        </span>
      </div>

      ${safeVerifierBanner}

      <!-- Key Numerical Metric Cards -->
      ${safeAnswerHtml}

      <!-- Detailed Analytical Narrative -->
      <div class="analysis-prose">
        ${safeAnalysisHtml}
      </div>

      ${safeDraftHtml}
    `;
    answerEl.appendChild(container);

    // Meta chips
    answerMeta.replaceChildren();
    const usage = data.usage || {};
    const metaItems = [
      { label: data.arm ? `${data.arm} arm` : "", type: "chip-accent" },
      { label: data.seconds !== undefined ? `${data.seconds}s` : "", type: "chip-neutral" },
      { label: usage.prompt_tokens !== undefined ? `${usage.prompt_tokens + usage.completion_tokens} tokens` : "", type: "chip-neutral" },
      { label: data.solver_steps !== undefined ? `${data.solver_steps} steps` : "", type: "chip-neutral" },
      { label: data.plan_source ? `plan: ${data.plan_source}` : "", type: "chip-success" },
    ].filter(i => Boolean(i.label));

    metaItems.forEach(item => {
      const chip = document.createElement("span");
      chip.className = `chip ${item.type}`;
      chip.textContent = item.label;
      answerMeta.appendChild(chip);
    });
  }

  function renderCharts(charts) {
    disposeCharts();
    chartsEl.replaceChildren();
    if (chartsPanelArea) chartsPanelArea.replaceChildren();

    if (!Array.isArray(charts) || charts.length === 0) {
      if (chartsPanelEmpty) chartsPanelEmpty.hidden = false;
      return;
    }

    if (chartsPanelEmpty) chartsPanelEmpty.hidden = true;
    if (chartsNavBadge) {
      chartsNavBadge.textContent = charts.length;
      chartsNavBadge.hidden = false;
    }

    for (const chart of charts) {
      const host = document.createElement("div");
      host.className = "chart-box";
      chartsEl.appendChild(host);

      if (typeof window.echarts !== "undefined") {
        const instance = window.echarts.init(host, "dark", { renderer: "canvas" });
        const opt = chart.option || chart;
        opt.backgroundColor = "transparent";
        instance.setOption(opt);
        chartInstances.push(instance);

        if (chartsPanelArea) {
          const panelHost = document.createElement("div");
          panelHost.className = "chart-box";
          chartsPanelArea.appendChild(panelHost);
          const panelInst = window.echarts.init(panelHost, "dark", { renderer: "canvas" });
          panelInst.setOption(opt);
          chartInstances.push(panelInst);
        }
      }
    }
  }

  function disposeReferenceCharts() {
    for (const chart of referenceChartInstances) {
      try { chart.dispose(); } catch (_) {}
    }
    referenceChartInstances = [];
  }

  function referenceNumber(value, digits) {
    if (typeof value !== "number" || !Number.isFinite(value)) return "n/a";
    return value.toFixed(digits);
  }

  function addReferenceCard(grid, label, value, unit, tag) {
    const card = document.createElement("div");
    card.className = "kpi-card";
    const labelEl = document.createElement("div");
    labelEl.className = "kpi-label";
    labelEl.textContent = label;
    const row = document.createElement("div");
    row.className = "kpi-value-row";
    const valueEl = document.createElement("span");
    valueEl.className = "kpi-num";
    valueEl.textContent = value;
    row.appendChild(valueEl);
    if (unit) {
      const unitEl = document.createElement("span");
      unitEl.className = "kpi-unit";
      unitEl.textContent = unit;
      row.appendChild(unitEl);
    }
    const tagEl = document.createElement("span");
    tagEl.className = "kpi-tag";
    tagEl.textContent = tag;
    card.appendChild(labelEl);
    card.appendChild(row);
    card.appendChild(tagEl);
    grid.appendChild(card);
  }

  function renderReferenceCharts(charts) {
    if (!referenceCharts) return;
    disposeReferenceCharts();
    referenceCharts.replaceChildren();
    if (!Array.isArray(charts)) return;
    for (const chart of charts) {
      const host = document.createElement("div");
      host.className = "reference-chart-box";
      referenceCharts.appendChild(host);
      if (typeof window.echarts !== "undefined") {
        const instance = window.echarts.init(host, "dark", { renderer: "canvas" });
        const opt = chart.option || chart;
        opt.backgroundColor = "transparent";
        instance.setOption(opt);
        referenceChartInstances.push(instance);
      }
    }
  }

  function renderReference(payload) {
    if (!referenceBody) return;
    const observed = payload.observed || {};
    const anomaly = payload.anomaly_assessment || {};
    const rawNo2 = anomaly.raw_ratio_by_pollutant ? anomaly.raw_ratio_by_pollutant.NO2 : anomaly.raw_ratio;

    referenceBody.replaceChildren();
    const grid = document.createElement("div");
    grid.className = "kpi-grid";
    addReferenceCard(grid, "NO2 at 20:00", referenceNumber(observed.no2_at_2000, 1), "ug/m3", "fixed window");
    addReferenceCard(grid, "Matched-hour z", referenceNumber(anomaly.matched_hour_z, 3), "", "paper ~1.9");
    addReferenceCard(grid, "Matched-hour max (14d)", referenceNumber(anomaly.matched_hour_max_prev_14d, 1), "ug/m3", "paper 125.5");
    addReferenceCard(grid, "Network NO2 rise (other 17)", referenceNumber(anomaly.network_mean_rise, 1), "ug/m3", "paper ~+19.6");
    addReferenceCard(grid, "Tai Po PM2.5 excess", referenceNumber(anomaly.nearest_station_rise_vs_network, 1), "ug/m3", "paper ~+7.4");
    addReferenceCard(grid, "PM2.5 rank of nearest", referenceNumber(anomaly.pm25_rank_of_nearest, 0), "of 18", "paper 1st");
    addReferenceCard(grid, "Raw ratio (NO2)", referenceNumber(rawNo2, 2) + "x", "", "screening only");
    addReferenceCard(grid, "Raw ratio (PM2.5)", referenceNumber(anomaly.raw_ratio_by_pollutant ? anomaly.raw_ratio_by_pollutant["PM2.5"] : null, 2) + "x", "", "screening only");
    addReferenceCard(grid, "Supports local plume", String(anomaly.supports_local_plume || "unknown"), "", "rule-based");
    referenceBody.appendChild(grid);

    const summary = document.createElement("p");
    summary.className = "reference-summary";
    summary.textContent = "Anomaly assessment: raw ratio " +
      referenceNumber(rawNo2, 2) + "x (NO2) vs matched-hour z " +
      referenceNumber(anomaly.matched_hour_z, 3) + "; network NO2 rise " +
      referenceNumber(anomaly.network_mean_rise, 1) + " ug/m3; supports_local_plume = " +
      String(anomaly.supports_local_plume || "unknown") + ".";
    referenceBody.appendChild(summary);

    renderReferenceCharts(payload.charts);
    if (referenceCaveat) {
      referenceCaveat.textContent = anomaly.caveat ||
        "The peak-to-mean ratio is a screening signal, not causal attribution.";
    }
    if (referenceStatus) {
      referenceStatus.textContent = payload.ok ? "Reference 21/21 checks" : "Reference degraded";
      referenceStatus.className = "chip " + (payload.ok ? "chip-success" : "chip-neutral");
    }
  }

  function renderReferenceError(message) {
    if (referenceBody) {
      referenceBody.replaceChildren();
      const paragraph = document.createElement("p");
      paragraph.className = "reference-summary";
      paragraph.textContent = "Reference unavailable: " + message;
      referenceBody.appendChild(paragraph);
    }
    if (referenceStatus) {
      referenceStatus.textContent = "Unavailable";
      referenceStatus.className = "chip chip-neutral";
    }
  }

  async function loadReference() {
    if (!referenceBody) return;
    try {
      const response = await fetch("/api/reference/tai_po");
      if (!response.ok) throw new Error("HTTP " + response.status);
      const payload = await response.json();
      renderReference(payload);
    } catch (error) {
      renderReferenceError(error && error.message ? error.message : String(error));
    }
  }

  async function handleEvent(name, data) {
    switch (name) {
      case "start":
        setStatus(`Running ${data.arm || "single"}...`, "running");
        addTrace("start", `Topology: ${data.arm || "single"} | Cap: ${data.timeout_seconds}s`);
        if (data.arm === "multi") {
          setActiveNode("coordinator", true);
          if (matrixTaskLabel) matrixTaskLabel.innerText = "COORDINATOR: Formulating multi-agent execution plan";
          if (hudText) hudText.innerText = "Coordinator decomposing query and provisioning token budget...";
          if (hudStep) hudStep.innerText = "Planning Phase";
        }
        break;

      case "plan":
        addTrace("plan", Array.isArray(data)
          ? data.map((step, index) => `${index + 1}. [${step.role}] ${step.task}`).join("\n")
          : text(data));
        break;

      case "worker_start":
        const role = data.role;
        const nodeKey = roleToNodeKey[role] || "data";
        
        if (activeRole && activeRole !== nodeKey) {
          setActiveNode(activeRole, false);
        }
        activeRole = nodeKey;
        roleHasReturned = false; // Reset for this worker
        
        addTrace("worker", `Active Role: ${role} (Budget: ${data.budget})\nTask: ${data.task || ""}`);
        
        if (matrixTaskLabel) matrixTaskLabel.innerText = `SUBTASK [${role}]: ${data.task || "Executing analysis..."}`;
        if (hudText) hudText.innerText = `Coordinator -> ${role}: Dispatching subtask assignment`;
        if (hudStep) hudStep.innerText = `Active: ${role}`;

        await animatePacket("coordinator", nodeKey, `CALL: ${role}`);
        setActiveNode(nodeKey, true);
        break;

      case "code":
        addTrace("python", text(data), true);
        if (hudText && activeRole) {
          hudText.innerText = `${activeRole}: Executing Python script in shared runtime...`;
        }
        break;

      case "output":
        addTrace("output", text(data), true);
        // Ensure strictly ONE return packet per worker execution
        if (activeRole && !roleHasReturned) {
          roleHasReturned = true;
          const retLabel = activeRole === "verifier" ? "RETURNS: audit" : "RETURNS: runtime_vars";
          await animatePacket(activeRole, "coordinator", retLabel, true);
        }
        break;

      case "final":
        clearAllActiveNodes();
        activeRole = null;
        roleHasReturned = false;
        if (matrixOverlay) matrixOverlay.classList.remove("active");
        if (data.verifier_audit_text) {
          addTrace("verifier", data.verifier_audit_text);
        }
        showAnswer(data);
        renderCharts(data.charts);
        setStatus(data.ok ? "Ready" : "Warning", data.ok ? "active" : "error");
        break;

      case "error":
        clearAllActiveNodes();
        if (matrixOverlay) matrixOverlay.classList.remove("active");
        errorEl.hidden = false;
        errorEl.textContent = `${data.type || "Error"}: ${data.message || "Failed"}`;
        setStatus("Error", "error");
        addTrace("error", `${data.type || "Error"}: ${data.message}`);
        break;

      case "done":
        addTrace("done", data.ok ? `Finished in ${data.seconds}s` : `Ended after ${data.seconds}s`);
        break;

      default:
        addTrace(name || "event", text(data));
    }
  }

  function consumeBlock(block) {
    let event = "message";
    const dataLines = [];
    for (const line of block.split(/\r?\n/)) {
      if (!line || line.startsWith(":")) continue;
      if (line.startsWith("event:")) {
        event = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).replace(/^ /, ""));
      }
    }
    if (dataLines.length === 0) return;
    const raw = dataLines.join("\n");
    let payload;
    try {
      payload = JSON.parse(raw);
    } catch (_) {
      payload = raw;
    }
    handleEvent(event, payload);
  }

  async function runQuery(event) {
    event.preventDefault();
    if (running) return;

    const query = queryInput.value.trim();
    if (!query) {
      queryInput.focus();
      return;
    }

    const armInput = document.querySelector("input[name='arm']:checked");
    const armValue = armInput ? armInput.value : "multi";

    resetOutput(armValue);
    setRunning(true, armValue === "multi");
    setStatus("Connecting...", "running");

    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, arm: armValue }),
      });

      if (response.status === 429) {
        if (matrixOverlay) matrixOverlay.classList.remove("active");
        answerEl.className = "answer-body empty";
        answerEl.innerHTML = `<p style="color:#fb7185">Another query is currently executing on the database runtime.</p>`;
        setStatus("Busy", "error");
        return;
      }

      if (!response.ok) {
        const body = await response.text();
        throw new Error(`HTTP ${response.status}: ${body.slice(0, 500)}`);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let boundary = buffer.indexOf("\n\n");
        while (boundary >= 0) {
          const block = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          consumeBlock(block);
          boundary = buffer.indexOf("\n\n");
        }
      }
      if (buffer.trim()) consumeBlock(buffer);
      setStatus("Ready", "active");
    } catch (error) {
      if (matrixOverlay) matrixOverlay.classList.remove("active");
      errorEl.hidden = false;
      errorEl.textContent = error.message || String(error);
      setStatus("Error", "error");
    } finally {
      setRunning(false, false);
    }
  }

  form.addEventListener("submit", runQuery);
  window.addEventListener("resize", () => {
    chartInstances.forEach(c => c.resize());
    referenceChartInstances.forEach(c => c.resize());
  });
  loadReference();
})();
