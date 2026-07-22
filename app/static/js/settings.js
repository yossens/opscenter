// "Settings" page logic: the list of stages in order from GET /api/stages,
// inline editing of the name and the waiting threshold (click -> input ->
// blur/Enter saves via PATCH /api/stages/{id}, Escape cancels -- pattern from
// deal.js), reordering with the up/down buttons and native HTML5 drag-and-drop
// of the whole row (pattern from board.js) -> POST /api/stages/reorder with the
// full list of ids, adding a stage at the end via a simple form -> POST
// /api/stages. The terminal stage ("Done") is edited like the others but is
// marked with a badge for clarity. All network requests are same-origin
// /api/... only, via window.OpsCenter.apiFetch.
"use strict";

(function () {
  const page = document.querySelector('[data-page="settings"]');
  if (!page) return;

  const listEl = page.querySelector("[data-stage-list]");
  const emptyEl = page.querySelector("[data-stage-empty]");
  const errorEl = page.querySelector("[data-settings-error]");

  const addToggleBtn = page.querySelector("[data-stage-add-toggle]");
  const addForm = page.querySelector("[data-stage-add-form]");
  const addCancelBtn = page.querySelector("[data-stage-add-cancel]");
  const addErrorEl = page.querySelector("[data-stage-add-error]");

  const pingForm = page.querySelector("[data-ping-settings-form]");
  const pingTemplateEl = page.querySelector("[data-ping-template]");
  const pingHiddenDaysEl = page.querySelector("[data-ping-hidden-days]");
  const pingResetBtn = page.querySelector("[data-ping-reset-default]");
  const pingErrorEl = page.querySelector("[data-ping-settings-error]");

  const parseForm = page.querySelector("[data-parse-settings-form]");
  const parseThresholdEl = page.querySelector("[data-parse-threshold]");
  const parseResetBtn = page.querySelector("[data-parse-reset-default]");
  const parseErrorEl = page.querySelector("[data-parse-settings-error]");
  let parseDefaultThreshold = null;

  // State: the current list of stages (in position order), plus the id of the
  // row currently being dragged (for drag-and-drop).
  let stages = [];
  let draggedRow = null;
  let pingDefaultTemplate = "";

  const showError = window.OpsCenter.makeErrorShower(errorEl);

  // friendlyErrorMessage: apiFetch puts into err.message either the detail
  // string (a normal HTTPException) or JSON.stringify of a list of pydantic
  // error objects (422 body validation details -- a list of {loc, msg, type}).
  // In the second case we extract the text of the first error so as not to show
  // the user unreadable JSON.
  function friendlyErrorMessage(err, fallback) {
    const message = (err && err.message) || fallback;
    if (typeof message !== "string" || !message.startsWith("[")) return message;
    try {
      const parsed = JSON.parse(message);
      if (Array.isArray(parsed) && parsed[0] && typeof parsed[0].msg === "string") {
        return parsed[0].msg;
      }
    } catch (_err) {
      /* not JSON -- show as is */
    }
    return message;
  }

  // ---------- Loading and rendering the list ----------

  async function loadStages() {
    try {
      stages = await window.OpsCenter.apiFetch("/api/stages");
    } catch (_err) {
      showError("Failed to load the list of stages");
      stages = [];
    }
    renderList();
  }

  function renderList() {
    listEl.innerHTML = "";
    if (!stages || stages.length === 0) {
      if (emptyEl) emptyEl.hidden = false;
      return;
    }
    if (emptyEl) emptyEl.hidden = true;
    const frag = document.createDocumentFragment();
    stages.forEach((stage, index) => {
      frag.appendChild(renderStageRow(stage, index));
    });
    listEl.appendChild(frag);
  }

  function renderStageRow(stage, index) {
    const row = document.createElement("div");
    row.className = "stage-row";
    row.draggable = true;
    row.dataset.stageId = String(stage.id);

    const handle = document.createElement("span");
    handle.className = "stage-drag-handle";
    handle.title = "Drag to reorder";
    handle.setAttribute("aria-hidden", "true");
    handle.textContent = "⋮⋮";
    row.appendChild(handle);

    const orderBtns = document.createElement("div");
    orderBtns.className = "stage-order-btns";

    const upBtn = document.createElement("button");
    upBtn.type = "button";
    upBtn.className = "stage-order-btn";
    upBtn.textContent = "↑";
    upBtn.setAttribute("aria-label", `Move “${stage.name}” up`);
    upBtn.disabled = index === 0;
    upBtn.addEventListener("click", () => moveStage(index, -1));
    orderBtns.appendChild(upBtn);

    const downBtn = document.createElement("button");
    downBtn.type = "button";
    downBtn.className = "stage-order-btn";
    downBtn.textContent = "↓";
    downBtn.setAttribute("aria-label", `Move “${stage.name}” down`);
    downBtn.disabled = index === stages.length - 1;
    downBtn.addEventListener("click", () => moveStage(index, 1));
    orderBtns.appendChild(downBtn);

    row.appendChild(orderBtns);

    const nameWrap = document.createElement("div");
    nameWrap.className = "stage-name-wrap";
    const nameValue = document.createElement("div");
    nameValue.className = "stage-name deal-field-value";
    nameValue.tabIndex = 0;
    nameValue.setAttribute("role", "button");
    nameValue.textContent = stage.name;
    nameValue.addEventListener("click", () => startEditName(row, nameValue, stage));
    nameValue.addEventListener("keydown", (event) => {
      if ((event.key === "Enter" || event.key === " ") && !nameValue.classList.contains("editing")) {
        event.preventDefault();
        startEditName(row, nameValue, stage);
      }
    });
    nameWrap.appendChild(nameValue);
    row.appendChild(nameWrap);

    const thresholdWrap = document.createElement("div");
    thresholdWrap.className = "stage-threshold-wrap";
    const thresholdLabel = document.createElement("span");
    thresholdLabel.className = "stage-threshold-label";
    thresholdLabel.textContent = "Threshold:";
    thresholdWrap.appendChild(thresholdLabel);

    const thresholdValue = document.createElement("div");
    thresholdValue.className = "stage-threshold deal-field-value";
    thresholdValue.tabIndex = 0;
    thresholdValue.setAttribute("role", "button");
    thresholdValue.textContent = String(stage.threshold_days);
    thresholdValue.addEventListener("click", () => startEditThreshold(row, thresholdValue, stage));
    thresholdValue.addEventListener("keydown", (event) => {
      if ((event.key === "Enter" || event.key === " ") && !thresholdValue.classList.contains("editing")) {
        event.preventDefault();
        startEditThreshold(row, thresholdValue, stage);
      }
    });
    thresholdWrap.appendChild(thresholdValue);

    const thresholdUnit = document.createElement("span");
    thresholdUnit.className = "stage-threshold-unit";
    thresholdUnit.textContent = "business days";
    thresholdWrap.appendChild(thresholdUnit);

    row.appendChild(thresholdWrap);

    const trackWrap = document.createElement("label");
    trackWrap.className = "stage-track-hangs-wrap";
    const trackCheckbox = document.createElement("input");
    trackCheckbox.type = "checkbox";
    trackCheckbox.className = "stage-track-hangs-checkbox";
    trackCheckbox.checked = Boolean(stage.track_hangs);
    // The terminal stage ("Done") never tracks hangs -- the server rejects a
    // track_hangs PATCH on it with 422 anyway, but disabled is more honest
    // (it prevents clicking a knowingly-rejected action).
    trackCheckbox.disabled = Boolean(stage.is_terminal);
    trackCheckbox.addEventListener("change", () => toggleTrackHangs(trackCheckbox, stage));
    trackWrap.appendChild(trackCheckbox);
    const trackText = document.createElement("span");
    trackText.textContent = "Track hangs";
    trackWrap.appendChild(trackText);
    row.appendChild(trackWrap);

    if (stage.is_terminal) {
      const badge = document.createElement("span");
      badge.className = "badge stage-terminal-badge";
      badge.textContent = "terminal";
      row.appendChild(badge);
    } else {
      // The terminal stage ("Done") cannot be deleted -- we don't show the
      // button, and the server additionally protects it (409).
      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "stage-delete-btn";
      delBtn.textContent = "×";
      delBtn.title = `Delete stage “${stage.name}”`;
      delBtn.setAttribute("aria-label", `Delete stage “${stage.name}”`);
      delBtn.addEventListener("click", () => deleteStage(stage));
      row.appendChild(delBtn);
    }

    attachRowDnd(row);

    return row;
  }

  // deleteStage: confirm -> DELETE /api/stages/{id} -> reload the list.
  // A non-empty/last working stage is rejected by the server (409) -- we show
  // the reason from detail without changing the list.
  async function deleteStage(stage) {
    if (!window.confirm(`Delete stage “${stage.name}”?`)) return;
    try {
      await window.OpsCenter.apiFetch(`/api/stages/${stage.id}`, { method: "DELETE" });
    } catch (err) {
      showError(err.message || "Failed to delete the stage");
      return;
    }
    await loadStages();
  }

  // ---------- Inline editing of the name and threshold (pattern from deal.js) ----------

  // startEditName: puts the name field into edit mode (input), focuses it, on
  // blur or Enter sends PATCH /api/stages/{id} with the new name, reverts on Escape.
  function startEditName(row, valueEl, stage) {
    if (valueEl.classList.contains("editing")) return;
    row.draggable = false;
    valueEl.classList.add("editing");
    valueEl.innerHTML = "";

    const input = document.createElement("input");
    input.type = "text";
    input.value = stage.name;
    input.className = "deal-field-input";
    valueEl.appendChild(input);
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);

    let settled = false;

    function finish() {
      valueEl.classList.remove("editing");
      row.draggable = true;
    }

    async function commit() {
      if (settled) return;
      settled = true;
      const name = input.value.trim();
      if (!name) {
        showError("Stage name cannot be empty");
        valueEl.textContent = stage.name;
        finish();
        return;
      }
      if (name === stage.name) {
        valueEl.textContent = stage.name;
        finish();
        return;
      }
      let updated;
      try {
        updated = await window.OpsCenter.apiFetch(`/api/stages/${stage.id}`, {
          method: "PATCH",
          body: { name },
        });
      } catch (err) {
        showError(err.message || "Failed to save the name");
        valueEl.textContent = stage.name;
        finish();
        return;
      }
      Object.assign(stage, updated);
      valueEl.textContent = stage.name;
      finish();
    }

    function cancel() {
      if (settled) return;
      settled = true;
      valueEl.textContent = stage.name;
      finish();
    }

    input.addEventListener("blur", commit);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        input.blur();
      } else if (event.key === "Escape") {
        event.preventDefault();
        cancel();
      }
    });
  }

  // startEditThreshold: puts the threshold field into edit mode (number),
  // focuses it, client-side validation (integer >= 1) prevents sending an
  // invalid value, on blur or Enter sends PATCH /api/stages/{id} with the new
  // threshold, reverts on Escape.
  // NOTE: the backend POST /api/stages does not validate threshold_days >= 1
  // (unlike PATCH) -- a known gap; the client compensates with validation, but
  // a third-party client could create an invalid stage.
  function startEditThreshold(row, valueEl, stage) {
    if (valueEl.classList.contains("editing")) return;
    row.draggable = false;
    valueEl.classList.add("editing");
    valueEl.innerHTML = "";

    const input = document.createElement("input");
    input.type = "number";
    input.min = "1";
    input.step = "1";
    input.value = String(stage.threshold_days);
    input.className = "deal-field-input";
    valueEl.appendChild(input);
    input.focus();

    let settled = false;

    function finish() {
      valueEl.classList.remove("editing");
      row.draggable = true;
    }

    async function commit() {
      if (settled) return;
      settled = true;
      const trimmed = input.value.trim();
      const num = Number(trimmed);
      // Client-side validation: integer >= 1, revert on error without a request.
      if (trimmed === "" || Number.isNaN(num) || !Number.isInteger(num) || num < 1) {
        showError("Threshold must be an integer of at least 1");
        valueEl.textContent = String(stage.threshold_days);
        finish();
        return;
      }
      if (num === stage.threshold_days) {
        valueEl.textContent = String(stage.threshold_days);
        finish();
        return;
      }
      let updated;
      try {
        updated = await window.OpsCenter.apiFetch(`/api/stages/${stage.id}`, {
          method: "PATCH",
          body: { threshold_days: num },
        });
      } catch (err) {
        showError(err.message || "Failed to save the threshold");
        valueEl.textContent = String(stage.threshold_days);
        finish();
        return;
      }
      Object.assign(stage, updated);
      valueEl.textContent = String(stage.threshold_days);
      finish();
    }

    function cancel() {
      if (settled) return;
      settled = true;
      valueEl.textContent = String(stage.threshold_days);
      finish();
    }

    input.addEventListener("blur", commit);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        input.blur();
      } else if (event.key === "Escape") {
        event.preventDefault();
        cancel();
      }
    });
  }

  // toggleTrackHangs: the "Track hangs" checkbox -> PATCH /api/stages/{id}
  // {track_hangs}. The terminal stage is protected by the checkbox's disabled
  // state (see renderStageRow), but the server still re-checks (422) -- in case
  // of state desync the checkbox reverts on error.
  async function toggleTrackHangs(checkbox, stage) {
    const next = checkbox.checked;
    checkbox.disabled = true;
    let updated;
    try {
      updated = await window.OpsCenter.apiFetch(`/api/stages/${stage.id}`, {
        method: "PATCH",
        body: { track_hangs: next },
      });
    } catch (err) {
      showError(err.message || "Failed to change hang tracking");
      checkbox.checked = !next;
      checkbox.disabled = Boolean(stage.is_terminal);
      return;
    }
    Object.assign(stage, updated);
    checkbox.checked = Boolean(stage.track_hangs);
    checkbox.disabled = Boolean(stage.is_terminal);
  }

  // ---------- Reordering: shared function to apply a new order ----------
  // applyReorder: optimistically updates the UI (stages, renderList), then sends
  // POST /api/stages/reorder with the full list of ids; on error it reverts
  // stages and the UI to previousStages and shows the error (while the up/down
  // buttons and dragend don't lose control thanks to the one-shot revert).
  async function applyReorder(newOrderedStages, previousStages) {
    const orderedIds = newOrderedStages.map((s) => s.id);
    stages = newOrderedStages;
    renderList();
    try {
      await window.OpsCenter.apiFetch("/api/stages/reorder", {
        method: "POST",
        body: { ordered_ids: orderedIds },
      });
    } catch (err) {
      showError(err.message || "Failed to change the stage order");
      stages = previousStages;
      renderList();
    }
  }

  // moveStage: swaps the stage at index with its neighbor (index+direction),
  // used by the up/down buttons.
  function moveStage(index, direction) {
    const targetIndex = index + direction;
    if (targetIndex < 0 || targetIndex >= stages.length) return;
    const previous = stages.slice();
    const next = stages.slice();
    const tmp = next[index];
    next[index] = next[targetIndex];
    next[targetIndex] = tmp;
    applyReorder(next, previous);
  }

  // ---------- Drag-and-drop of the whole row (pattern from board.js) ----------

  // attachRowDnd: attaches dragstart/dragend listeners to a stage row for
  // dragging between positions; dragend compares the new DOM row order with the
  // current stages list, calls applyReorder to sync with the server and revert on error.
  function getRowAfter(container, clientY) {
    const rows = Array.from(container.querySelectorAll(".stage-row:not(.dragging)"));
    let closest = null;
    let closestOffset = Number.NEGATIVE_INFINITY;
    for (const el of rows) {
      const box = el.getBoundingClientRect();
      const offset = clientY - box.top - box.height / 2;
      if (offset < 0 && offset > closestOffset) {
        closestOffset = offset;
        closest = el;
      }
    }
    return closest;
  }

  function attachRowDnd(row) {
    row.addEventListener("dragstart", (event) => {
      draggedRow = row;
      row.classList.add("dragging");
      if (event.dataTransfer) {
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", row.dataset.stageId);
      }
    });

    row.addEventListener("dragend", async () => {
      row.classList.remove("dragging");
      const dragged = draggedRow;
      draggedRow = null;
      if (!dragged) return;

      const newOrderIds = Array.from(listEl.querySelectorAll(".stage-row")).map((el) =>
        Number(el.dataset.stageId)
      );
      const previous = stages.slice();
      const oldOrderIds = previous.map((s) => s.id);
      if (JSON.stringify(newOrderIds) === JSON.stringify(oldOrderIds)) return;

      const byId = new Map(previous.map((s) => [s.id, s]));
      const next = newOrderIds.map((id) => byId.get(id)).filter(Boolean);
      if (next.length !== previous.length) {
        // DOM/state consistency is broken -- safer to reload from the server.
        await loadStages();
        return;
      }
      await applyReorder(next, previous);
    });
  }

  listEl.addEventListener("dragover", (event) => {
    if (!draggedRow) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
    const afterEl = getRowAfter(listEl, event.clientY);
    if (afterEl == null) {
      listEl.appendChild(draggedRow);
    } else if (afterEl !== draggedRow) {
      listEl.insertBefore(draggedRow, afterEl);
    }
  });

  // ---------- Add-stage form ----------
  // The add form: simple (name, optional threshold_days with client-side
  // validation >= 1), sends POST /api/stages, then reloads the list (loadStages)
  // and closes the form. An invalid threshold is rejected on the client with an
  // error message (protection against invalid values, since the backend POST
  // does not validate threshold_days server-side).

  function openAddForm() {
    if (!addForm) return;
    addForm.hidden = false;
    if (addErrorEl) addErrorEl.hidden = true;
    addForm.reset();
    const nameInput = addForm.querySelector('[name="name"]');
    if (nameInput) nameInput.focus();
  }

  function closeAddForm() {
    if (!addForm) return;
    addForm.hidden = true;
  }

  if (addToggleBtn) {
    addToggleBtn.addEventListener("click", () => {
      if (addForm && !addForm.hidden) {
        closeAddForm();
      } else {
        openAddForm();
      }
    });
  }
  if (addCancelBtn) addCancelBtn.addEventListener("click", closeAddForm);

  if (addForm) {
    addForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = new FormData(addForm);
      const name = String(data.get("name") || "").trim();
      if (!name) {
        if (addErrorEl) {
          addErrorEl.textContent = "Name is required";
          addErrorEl.hidden = false;
        }
        return;
      }
      const payload = { name };
      const thresholdRaw = String(data.get("threshold_days") || "").trim();
      if (thresholdRaw !== "") {
        const num = Number(thresholdRaw);
        if (Number.isNaN(num) || !Number.isInteger(num) || num < 1) {
          if (addErrorEl) {
            addErrorEl.textContent = "Threshold must be an integer of at least 1";
            addErrorEl.hidden = false;
          }
          return;
        }
        payload.threshold_days = num;
      }
      try {
        await window.OpsCenter.apiFetch("/api/stages", { method: "POST", body: payload });
      } catch (err) {
        if (addErrorEl) {
          addErrorEl.textContent = err.message || "Failed to add the stage";
          addErrorEl.hidden = false;
        }
        return;
      }
      closeAddForm();
      await loadStages();
    });
  }

  // ---------- Hang detector settings: ping template, M ----------
  // loadPingSettings: GET /api/settings/ping -> fills the textarea/M field with
  // the current values (via .value -- safe, no innerHTML/HTML parsing) and
  // remembers default_template for the "Reset to default" button.
  function showPingError(message) {
    if (!pingErrorEl) return;
    pingErrorEl.textContent = message;
    pingErrorEl.hidden = false;
  }

  function hidePingError() {
    if (pingErrorEl) pingErrorEl.hidden = true;
  }

  async function loadPingSettings() {
    if (!pingForm) return;
    try {
      const settings = await window.OpsCenter.apiFetch("/api/settings/ping");
      pingDefaultTemplate = settings.default_template || "";
      if (pingTemplateEl) pingTemplateEl.value = settings.template || "";
      if (pingHiddenDaysEl) pingHiddenDaysEl.value = String(settings.hidden_days);
    } catch (_err) {
      showPingError("Failed to load the hang detector settings");
    }
  }

  // "Reset to default": only fills default_template into the textarea -- saving
  // to app_meta happens exclusively on the "Save" click, this press does not
  // hit the network.
  if (pingResetBtn) {
    pingResetBtn.addEventListener("click", () => {
      if (pingTemplateEl) pingTemplateEl.value = pingDefaultTemplate;
    });
  }

  if (pingForm) {
    pingForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      hidePingError();
      const template = pingTemplateEl ? pingTemplateEl.value : "";
      const hiddenDaysRaw = pingHiddenDaysEl ? pingHiddenDaysEl.value.trim() : "";
      const hiddenDays = Number(hiddenDaysRaw);
      if (!template.trim()) {
        showPingError("The ping template cannot be empty");
        return;
      }
      if (hiddenDaysRaw === "" || Number.isNaN(hiddenDays) || !Number.isInteger(hiddenDays) || hiddenDays < 0) {
        showPingError("M must be an integer of at least 0");
        return;
      }
      try {
        await window.OpsCenter.apiFetch("/api/settings/ping", {
          method: "PUT",
          body: { template, hidden_days: hiddenDays },
        });
      } catch (err) {
        showPingError(friendlyErrorMessage(err, "Failed to save the hang detector settings"));
        return;
      }
      await loadPingSettings();
    });
  }

  // ---------- LLM parsing: cost stats + confidence threshold ----------
  // All stat values are inserted via textContent (numbers from /api/llm/stats),
  // the threshold is read/written via GET/PUT /api/settings/parse. Same-origin /api/... only.

  function showParseError(message) {
    if (!parseErrorEl) return;
    parseErrorEl.textContent = message;
    parseErrorEl.hidden = false;
  }

  function hideParseError() {
    if (parseErrorEl) parseErrorEl.hidden = true;
  }

  function setStat(key, value) {
    const el = page.querySelector(`[data-stat="${key}"]`);
    if (el) el.textContent = value;
  }

  function formatCost(usd) {
    const num = typeof usd === "number" ? usd : 0;
    return `$${num.toFixed(4)}`;
  }

  function fillStatsColumn(prefix, bucket) {
    const data = bucket || {};
    setStat(`${prefix}-calls`, String(data.calls || 0));
    setStat(`${prefix}-input`, String(data.input_tokens || 0));
    setStat(`${prefix}-output`, String(data.output_tokens || 0));
    setStat(`${prefix}-cost`, formatCost(data.cost_usd));
  }

  async function loadLlmStats() {
    try {
      const stats = await window.OpsCenter.apiFetch("/api/llm/stats");
      fillStatsColumn("today", stats.today);
      fillStatsColumn("m30", stats.last_30_days);
    } catch (_err) {
      showParseError("Failed to load the LLM parsing stats");
    }
  }

  async function loadParseSettings() {
    if (!parseForm) return;
    try {
      const settings = await window.OpsCenter.apiFetch("/api/settings/parse");
      parseDefaultThreshold =
        typeof settings.default_confidence_threshold === "number"
          ? settings.default_confidence_threshold
          : null;
      if (parseThresholdEl && typeof settings.confidence_threshold === "number") {
        parseThresholdEl.value = String(settings.confidence_threshold);
      }
    } catch (_err) {
      showParseError("Failed to load the LLM parsing settings");
    }
  }

  // "Reset to default": only fills the value into the field (like the ping one),
  // saving happens via the "Save" button.
  if (parseResetBtn) {
    parseResetBtn.addEventListener("click", () => {
      if (parseThresholdEl && parseDefaultThreshold != null) {
        parseThresholdEl.value = String(parseDefaultThreshold);
      }
    });
  }

  if (parseForm) {
    parseForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      hideParseError();
      const raw = parseThresholdEl ? parseThresholdEl.value.trim() : "";
      const value = Number(raw);
      if (raw === "" || Number.isNaN(value) || value < 0 || value > 1) {
        showParseError("The threshold must be a number between 0 and 1");
        return;
      }
      try {
        await window.OpsCenter.apiFetch("/api/settings/parse", {
          method: "PUT",
          body: { confidence_threshold: value },
        });
      } catch (err) {
        showParseError(friendlyErrorMessage(err, "Failed to save the confidence threshold"));
        return;
      }
      await loadParseSettings();
    });
  }

  loadStages();
  loadPingSettings();
  loadLlmStats();
  loadParseSettings();
})();
