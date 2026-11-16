/* Neuro-Air Lite UI: POST the question, consume SSE, render the trace and charts. */
(() => {
  "use strict";

  const form = document.getElementById("chat-form");
  const queryInput = document.getElementById("query");
  const armSelect = document.getElementById("arm");
  const sendButton = document.getElementById("send");
  const statusEl = document.getElementById("status");
  const answerEl = document.getElementById("answer");
  const answerMeta = document.getElementById("answer-meta");
  const chartsEl = document.getElementById("charts");
  const errorEl = document.getElementById("error");
  const traceEl = document.getElementById("trace");
  const traceMeta = document.getElementById("trace-meta");

  let chartInstances = [];
  let running = false;

  function setStatus(text, mode = "") {
    statusEl.textContent = text;
    statusEl.className = `status ${mode}`.trim();
  }

  function setRunning(value) {
    running = value;
    sendButton.disabled = value;
    armSelect.disabled = value;
    queryInput.disabled = value;
  }

  function resetOutput() {
    answerEl.className = "answer empty";
    answerEl.textContent = "Working…";
    answerMeta.textContent = "";
    chartsEl.replaceChildren();
    traceEl.replaceChildren();
    traceMeta.textContent = "";
    errorEl.hidden = true;
    errorEl.textContent = "";
    disposeCharts();
  }

  function disposeCharts() {
    for (const chart of chartInstances) {
      try { chart.dispose(); } catch (_) { /* already disposed */ }
    }
    chartInstances = [];
  }

  function addTrace(kind, text, code = false) {
    const item = document.createElement("li");
    const label = document.createElement("strong");
    label.textContent = kind;
    item.appendChild(label);

    if (code) {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = "show";
      const pre = document.createElement("pre");
      pre.textContent = text;
      details.appendChild(summary);
      details.appendChild(pre);
      item.appendChild(details);
    } else {
      const pre = document.createElement("pre");
      pre.textContent = text;
      item.appendChild(pre);
    }

    traceEl.appendChild(item);
    traceEl.scrollIntoView({ block: "nearest", behavior: "smooth" });
    traceMeta.textContent = `${traceEl.children.length} event${traceEl.children.length === 1 ? "" : "s"}`;
  }

  function text(value) {
    if (value === null || value === undefined) return "";
    if (typeof value === "string") return value;
    try { return JSON.stringify(value); } catch (_) { return String(value); }
  }

  function showAnswer(data) {
    answerEl.className = "answer";
    answerEl.replaceChildren();

    const answer = data.answer;
    if (answer !== null && answer !== undefined && answer !== "") {
      const badge = document.createElement("span");
      badge.className = "answer-value";
      badge.textContent = `ANSWER = ${text(answer)}`;
      answerEl.appendChild(badge);
      answerEl.appendChild(document.createElement("br"));
    }

    const body = document.createElement("span");
    body.textContent = data.final_text || "(no final text)";
    answerEl.appendChild(body);

    const usage = data.usage || {};
    const parts = [
      data.arm ? `${data.arm} arm` : "",
      data.seconds !== undefined ? `${data.seconds}s` : "",
      usage.prompt_tokens !== undefined
        ? `${usage.prompt_tokens + usage.completion_tokens} tokens`
        : "",
      usage.calls !== undefined ? `${usage.calls} calls` : "",
      data.solver_steps !== undefined ? `${data.solver_steps} steps` : "",
      data.plan_source ? `plan: ${data.plan_source}` : "",
    ].filter(Boolean);
    answerMeta.textContent = parts.join(" · ");
  }

  function renderCharts(charts) {
    disposeCharts();
    chartsEl.replaceChildren();
    if (!Array.isArray(charts) || charts.length === 0) return;
    if (typeof window.echarts === "undefined") {
      const warning = document.createElement("p");
      warning.className = "error";
      warning.textContent = "ECharts did not load, so the chart data is shown below.";
      chartsEl.appendChild(warning);
      for (const chart of charts) {
        const pre = document.createElement("pre");
        pre.textContent = text(chart.option || chart);
        chartsEl.appendChild(pre);
      }
      return;
    }

    for (const chart of charts) {
      const host = document.createElement("div");
      host.className = "chart";
      host.setAttribute("role", "img");
      host.setAttribute("aria-label", chart.title || "chart");
      chartsEl.appendChild(host);
      const instance = window.echarts.init(host);
      instance.setOption(chart.option || chart);
      chartInstances.push(instance);
    }
  }

  function handleEvent(name, data) {
    switch (name) {
      case "start":
        setStatus(`Running ${data.arm || "single"} arm…`, "running");
        addTrace("start", `${data.arm || "single"} · ${data.model || "model"} · cap ${data.timeout_seconds}s`);
        break;
      case "plan":
        addTrace("plan", Array.isArray(data)
          ? data.map((step, index) => `${index + 1}. ${step.role}: ${step.task}`).join("\n")
          : text(data));
        break;
      case "worker_start":
        addTrace("worker", `${data.role} · budget ${data.budget}\n${data.task || ""}`);
        break;
      case "worker_stall":
        addTrace("stall", `Stopped a repeated code block in ${data.role}.`);
        break;
      case "code":
        addTrace("python", text(data), true);
        break;
      case "output":
        addTrace("output", text(data), true);
        break;
      case "agent_final":
        addTrace("agent final", text(data));
        break;
      case "final":
        showAnswer(data);
        renderCharts(data.charts);
        setStatus(data.ok ? "Finished" : "Finished with a warning", data.ok ? "" : "error");
        break;
      case "error":
        errorEl.hidden = false;
        errorEl.textContent = `${data.type || "Error"}: ${data.message || "request failed"}`;
        setStatus("Error", "error");
        addTrace("error", `${data.type || "Error"}: ${data.message || "request failed"}`);
        break;
      case "done":
        addTrace("done", data.ok ? `Complete in ${data.seconds}s` : `Stopped after ${data.seconds}s`);
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

    resetOutput();
    setRunning(true);
    setStatus("Connecting…", "running");

    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, arm: armSelect.value }),
      });

      if (response.status === 429) {
        answerEl.className = "answer empty";
        answerEl.textContent = "Another query is already running.";
        setStatus("Busy", "error");
        return;
      }

      if (!response.ok) {
        const body = await response.text();
        throw new Error(`HTTP ${response.status}: ${body.slice(0, 500)}`);
      }
      if (!response.body) {
        throw new Error("This browser cannot read the streaming response.");
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
      if (statusEl.classList.contains("running")) setStatus("Done");
    } catch (error) {
      errorEl.hidden = false;
      errorEl.textContent = error.message || String(error);
      setStatus("Error", "error");
    } finally {
      setRunning(false);
    }
  }

  form.addEventListener("submit", runQuery);
  window.addEventListener("resize", () => {
    for (const chart of chartInstances) {
      try { chart.resize(); } catch (_) { /* disposed */ }
    }
  });
})();
