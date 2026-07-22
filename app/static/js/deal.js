// Item card page logic: inline-editable fields, automatic values (dates/aging
// are display-only), a chronological feed of attached notes with attachments,
// and quick note dropping straight into the item (reuses the quick-drop pattern
// from inbox.js: Enter/Shift+Enter/Ctrl+V/drag-and-drop). Service "ping"
// entries (deal.pings) from the hang detector are merged into the feed by time
// -- see renderFeed/mergeFeedItems/renderPingEntry. The item's drive/llm fields
// are not shown on the page at all. All network requests are same-origin
// /api/... only, via window.OpsCenter.apiFetch.
"use strict";

(function () {
  const page = document.querySelector('[data-page="deal"]');
  if (!page) return;

  // deal_id is taken from the page URL (/deals/{id}), not from the template.
  const match = window.location.pathname.match(/\/deals\/(\d+)/);
  const dealId = match ? Number(match[1]) : null;

  const titleEl = page.querySelector("[data-deal-title]");
  const daysBadge = page.querySelector("[data-days-badge]");
  const stageSelect = page.querySelector("[data-deal-stage-select]");
  const errorEl = page.querySelector("[data-deal-error]");
  const notFoundEl = page.querySelector("[data-deal-not-found]");
  const layoutEl = page.querySelector("[data-deal-layout]");
  const stageEnteredEl = page.querySelector("[data-stage-entered-at]");
  const lastActivityEl = page.querySelector("[data-last-activity-at]");
  const createdAtEl = page.querySelector("[data-created-at]");
  const feedEl = page.querySelector("[data-note-feed]");
  const textarea = page.querySelector("[data-quick-drop]");
  const dropZone = page.querySelector("[data-drop-zone]");
  const dropErrorEl = page.querySelector("[data-drop-error]");

  const fieldEls = Array.from(page.querySelectorAll("[data-field]"));

  if (!dealId) {
    if (notFoundEl) notFoundEl.hidden = false;
    if (layoutEl) layoutEl.hidden = true;
    return;
  }

  let currentDeal = null;

  function badgeClassForAging(level) {
    if (level === "warn") return "aging-warn";
    if (level === "overdue") return "aging-overdue";
    return "aging-ok";
  }

  const showError = window.OpsCenter.makeErrorShower(errorEl);
  const showDropError = window.OpsCenter.makeErrorShower(dropErrorEl);

  // ---------- Attachments and the note card in the feed (pattern from inbox.js) ----------
  // TODO: renderAttachment() and renderNoteCard() duplicate logic from inbox.js.
  // If more such functions appear, consider extracting them into a shared JS
  // module (e.g. app/static/js/components.js) for DRY. For now the same
  // duplication pattern as in board.js is applied.

  // renderPingEntry: a service "ping" entry in the item card feed --
  // GET /api/deals/{id} returns them as a separate pings array (pings are not
  // notes); the feed merges them chronologically with the notes. ping_text is
  // user text (may contain {last_note} from another note), inserted EXCLUSIVELY
  // via textContent, never innerHTML. Visually distinct from a normal note via
  // the ping-entry class (a muted service style, app.css) and a "ping, step N" label.
  function renderPingEntry(ping) {
    const card = document.createElement("article");
    card.className = "note-card ping-entry";
    card.dataset.pingId = String(ping.id);

    const bodyEl = document.createElement("p");
    bodyEl.className = "note-body ping-entry-text";
    bodyEl.textContent = ping.ping_text;
    card.appendChild(bodyEl);

    const meta = document.createElement("div");
    meta.className = "note-meta";

    const time = document.createElement("span");
    time.className = "note-time";
    time.textContent = window.OpsCenter.formatDate(ping.pinged_at);
    meta.appendChild(time);

    const type = document.createElement("span");
    type.className = "note-type-badge ping-entry-badge";
    type.textContent = `ping, step ${ping.escalation_step}`;
    meta.appendChild(type);

    card.appendChild(meta);
    return card;
  }

  // renderNoteCard: assembles a note card in the feed (body, attachments,
  // metadata with date and type). The feed is chronological (oldest on top),
  // unlike the Inbox.
  function renderNoteCard(note) {
    const card = document.createElement("article");
    card.className = "note-card";
    card.dataset.noteId = String(note.id);

    if (note.body) {
      const bodyEl = document.createElement("p");
      bodyEl.className = "note-body";
      // Escape + linkify links; innerHTML is safe because linkifyEscaped
      // escapes the text BEFORE inserting <a>.
      bodyEl.innerHTML = window.OpsCenter.linkifyEscaped(note.body);
      card.appendChild(bodyEl);
    }

    if (note.attachments && note.attachments.length) {
      const attWrap = document.createElement("div");
      attWrap.className = "note-attachments";
      for (const att of note.attachments) {
        attWrap.appendChild(window.OpsCenter.renderAttachment(att));
      }
      card.appendChild(attWrap);
    }

    // OCR text: LLM output, rendered as plain text (textContent), not
    // linkified and not rendered as HTML (stored-XSS risk).
    if (note.ocr_text) {
      const ocr = document.createElement("div");
      ocr.className = "note-ocr-text";
      ocr.textContent = note.ocr_text;
      card.appendChild(ocr);
    }

    const meta = document.createElement("div");
    meta.className = "note-meta";
    const time = document.createElement("span");
    time.className = "note-time";
    time.textContent = window.OpsCenter.formatDate(note.created_at);
    meta.appendChild(time);
    if (note.note_type) {
      const type = document.createElement("span");
      type.className = "note-type-badge";
      type.textContent = window.OpsCenter.NOTE_TYPE_LABELS[note.note_type] || note.note_type;
      meta.appendChild(type);
    }

    // Deleting a note from the feed: confirm -> DELETE /api/notes/{id}
    // (cascades attachment rows + files on disk, see router) -> a full card
    // re-fetch to refresh last_activity_at.
    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "small danger note-delete";
    delBtn.textContent = "Delete";
    delBtn.addEventListener("click", async () => {
      if (!confirm("Delete this note? Its attachments will be permanently deleted.")) return;
      try {
        await window.OpsCenter.apiFetch(`/api/notes/${note.id}`, { method: "DELETE" });
      } catch (err) {
        showError(err.message || "Failed to delete the note");
        return;
      }
      window.OpsCenter.refreshInboxCounter();
      await loadDeal();
    });
    meta.appendChild(delBtn);

    card.appendChild(meta);

    return card;
  }

  // mergeFeedItems: merges notes (created_at) and pings (pinged_at) into one
  // chronological list. Both fields are UTC ISO YYYY-MM-DDTHH:MM:SS (the same
  // guarantee that pings_since relies on in app/repo/pings.py), so plain
  // lexicographic string comparison gives the correct order without parsing dates.
  function mergeFeedItems(notes, pings) {
    const items = [];
    for (const note of notes || []) {
      items.push({ kind: "note", data: note, sortKey: note.created_at || "" });
    }
    for (const ping of pings || []) {
      items.push({ kind: "ping", data: ping, sortKey: ping.pinged_at || "" });
    }
    items.sort((a, b) => {
      if (a.sortKey < b.sortKey) return -1;
      if (a.sortKey > b.sortKey) return 1;
      return 0;
    });
    return items;
  }

  // The card feed is chronological (oldest on top, as returned by
  // GET /api/deals/{id}: notes ORDER BY created_at ASC), unlike the Inbox where
  // newest is on top. Service "ping" entries (deal.pings) are merged into the
  // feed, rendered by renderPingEntry -- visually distinct from notes.
  function renderFeed(deal) {
    feedEl.innerHTML = "";
    const items = mergeFeedItems(deal ? deal.notes : null, deal ? deal.pings : null);
    if (!items.length) {
      const empty = document.createElement("p");
      empty.className = "page-hint note-feed-empty";
      empty.textContent = "No notes yet.";
      feedEl.appendChild(empty);
      return;
    }
    const frag = document.createDocumentFragment();
    for (const item of items) {
      frag.appendChild(item.kind === "ping" ? renderPingEntry(item.data) : renderNoteCard(item.data));
    }
    feedEl.appendChild(frag);
  }

  // ---------- Inline editing of card fields ----------
  // Clicking a field value turns it into an input/textarea; blur or Enter
  // (Ctrl+Enter for textarea so Enter can insert a newline) saves via
  // PATCH /api/deals/{id}; Escape cancels without saving.
  // title is not included here: the backend explicitly forbids changing title
  // via PATCH -- the title is shown as a static page heading.

  function displayValue(key, value) {
    return value ? String(value) : "—";
  }

  // renderFieldValue: updates the displayed field value (formatting, leaving
  // edit mode, reflecting the current value from currentDeal).
  function renderFieldValue(valueEl, key) {
    valueEl.classList.remove("editing");
    valueEl.textContent = displayValue(key, currentDeal ? currentDeal[key] : null);
  }

  function renderAllFields() {
    for (const fieldEl of fieldEls) {
      const key = fieldEl.dataset.field;
      const valueEl = fieldEl.querySelector("[data-field-value]");
      renderFieldValue(valueEl, key);
    }
  }

  // saveField: sends PATCH /api/deals/{id} to save one field, updates
  // currentDeal and recomputes the automatic fields (dates, aging).
  async function saveField(key, payloadValue, valueEl) {
    let updated;
    try {
      updated = await window.OpsCenter.apiFetch(`/api/deals/${dealId}`, {
        method: "PATCH",
        body: { [key]: payloadValue },
      });
    } catch (err) {
      showError(err.message || "Failed to save the field");
      renderFieldValue(valueEl, key);
      return;
    }
    currentDeal = Object.assign(currentDeal || {}, updated);
    renderFieldValue(valueEl, key);
    renderAutomatic();
  }

  function startEdit(fieldEl) {
    const key = fieldEl.dataset.field;
    const fieldType = fieldEl.dataset.fieldType || "text";
    const valueEl = fieldEl.querySelector("[data-field-value]");
    if (valueEl.classList.contains("editing")) return;

    const raw = currentDeal ? currentDeal[key] : null;
    valueEl.classList.add("editing");
    valueEl.innerHTML = "";

    let input;
    if (fieldType === "textarea") {
      input = document.createElement("textarea");
      input.rows = 3;
      input.value = raw || "";
    } else if (fieldType === "number") {
      input = document.createElement("input");
      input.type = "number";
      input.step = "any";
      input.value = raw === null || raw === undefined ? "" : String(raw);
    } else {
      input = document.createElement("input");
      input.type = "text";
      input.value = raw || "";
    }
    input.className = "deal-field-input";
    valueEl.appendChild(input);
    input.focus();
    if (fieldType !== "number" && typeof input.setSelectionRange === "function") {
      const end = input.value.length;
      input.setSelectionRange(end, end);
    }

    let settled = false;

    // commit(): moves the edited field into save mode. Validates numeric
    // fields, sends PATCH, or shows a validation error. The settled flag
    // prevents a double trigger (blur + Enter at once).
    function commit() {
      if (settled) return;
      settled = true;
      const rawValue = input.value;
      if (fieldType === "number") {
        const trimmed = rawValue.trim();
        if (trimmed === "") {
          saveField(key, null, valueEl);
          return;
        }
        const num = Number(trimmed);
        if (Number.isNaN(num)) {
          showError("Value must be a number");
          renderFieldValue(valueEl, key);
          return;
        }
        saveField(key, num, valueEl);
        return;
      }
      saveField(key, rawValue, valueEl);
    }

    // cancel(): cancels editing without saving (triggered on Escape).
    // Returns the field to display mode with the original value from currentDeal.
    function cancel() {
      if (settled) return;
      settled = true;
      renderFieldValue(valueEl, key);
    }

    input.addEventListener("blur", commit);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && fieldType !== "textarea") {
        event.preventDefault();
        input.blur();
      } else if (event.key === "Enter" && fieldType === "textarea" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        input.blur();
      } else if (event.key === "Escape") {
        event.preventDefault();
        cancel();
      }
    });
  }

  for (const fieldEl of fieldEls) {
    const valueEl = fieldEl.querySelector("[data-field-value]");
    valueEl.addEventListener("click", () => startEdit(fieldEl));
    valueEl.addEventListener("keydown", (event) => {
      if ((event.key === "Enter" || event.key === " ") && !valueEl.classList.contains("editing")) {
        event.preventDefault();
        startEdit(fieldEl);
      }
    });
  }

  // ---------- Automatic: dates and aging -- display only, not editable ----------

  function renderAutomatic() {
    if (!currentDeal) return;
    if (stageEnteredEl) stageEnteredEl.textContent = window.OpsCenter.formatDate(currentDeal.stage_entered_at) || "—";
    if (lastActivityEl) lastActivityEl.textContent = window.OpsCenter.formatDate(currentDeal.last_activity_at) || "—";
    if (createdAtEl) createdAtEl.textContent = window.OpsCenter.formatDate(currentDeal.created_at) || "—";
    if (daysBadge) {
      daysBadge.hidden = false;
      daysBadge.className = `badge ${badgeClassForAging(currentDeal.aging_level)}`;
      daysBadge.textContent = `${currentDeal.days_in_stage} business days`;
    }
  }

  // ---------- Stage: a select of stages + moving via /move ----------

  async function loadStages() {
    let stages;
    try {
      stages = await window.OpsCenter.apiFetch("/api/stages");
    } catch (_err) {
      stages = [];
    }
    if (!stageSelect) return;
    stageSelect.innerHTML = "";
    for (const stage of stages) {
      const option = document.createElement("option");
      option.value = String(stage.id);
      option.textContent = stage.name;
      stageSelect.appendChild(option);
    }
    if (currentDeal) stageSelect.value = String(currentDeal.stage_id);
  }

  if (stageSelect) {
    stageSelect.addEventListener("change", async () => {
      const stageId = Number(stageSelect.value);
      try {
        await window.OpsCenter.apiFetch(`/api/deals/${dealId}/move`, {
          method: "POST",
          body: { stage_id: stageId },
        });
      } catch (err) {
        showError(err.message || "Failed to move the item");
        if (currentDeal) stageSelect.value = String(currentDeal.stage_id);
        return;
      }
      await loadDeal();
    });
  }

  // ---------- Loading the card ----------

  async function loadDeal() {
    let deal;
    try {
      deal = await window.OpsCenter.apiFetch(`/api/deals/${dealId}`);
    } catch (err) {
      if (err.status === 404) {
        if (notFoundEl) notFoundEl.hidden = false;
        if (layoutEl) layoutEl.hidden = true;
        return;
      }
      showError(err.message || "Failed to load the item");
      return;
    }
    currentDeal = deal;
    if (notFoundEl) notFoundEl.hidden = true;
    if (layoutEl) layoutEl.hidden = false;
    if (titleEl) titleEl.textContent = deal.title;
    document.title = `${deal.title} — OpsCenter`;
    renderAllFields();
    renderAutomatic();
    renderFeed(deal);
    if (stageSelect) stageSelect.value = String(deal.stage_id);
  }

  // ---------- Quick drop straight into the card (pattern from inbox.js) ----------
  // Difference from the Inbox: deal_id is passed right away, the note is created
  // attached (status='attached') and never appears in the "Inbox".

  // submitNote: sends a note with body and/or files, attached to the current item.
  // Returns null if the note is empty (no text and no files) -- no request is made.
  async function submitNote(bodyText, files) {
    const text = (bodyText || "").trim();
    const fileList = files || [];
    if (!text && fileList.length === 0) return null; // empty drop -- no request

    const formData = new FormData();
    formData.append("body", bodyText || "");
    formData.append("deal_id", String(dealId));
    for (const file of fileList) {
      formData.append("files", file, file.name);
    }
    return window.OpsCenter.apiFetch("/api/notes", { method: "POST", body: formData });
  }

  async function handleSubmit(bodyText, files) {
    let note;
    try {
      note = await submitNote(bodyText, files);
    } catch (err) {
      showDropError(err.message || "Failed to submit the note");
      return;
    }
    if (!note) return; // empty drop -- there was no request

    if (textarea) textarea.value = "";

    if (currentDeal) {
      currentDeal.notes = currentDeal.notes || [];
      currentDeal.notes.push(note);
      renderFeed(currentDeal);
      currentDeal.last_activity_at = note.created_at;
      renderAutomatic();
    }

    window.OpsCenter.refreshInboxCounter();
    if (textarea) textarea.focus();
  }

  if (textarea) {
    textarea.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" || event.shiftKey) return;
      event.preventDefault();
      const text = textarea.value;
      if (!text.trim()) return; // empty Enter -- no request
      handleSubmit(text, []);
    });

    textarea.addEventListener("paste", (event) => {
      const items = event.clipboardData ? Array.from(event.clipboardData.items) : [];
      const files = items
        .filter((item) => item.kind === "file")
        .map((item) => item.getAsFile())
        .filter(Boolean);
      if (files.length === 0) return; // normal text paste -- default behavior
      event.preventDefault();
      const text = textarea.value;
      handleSubmit(text, files);
    });
  }

  let dragCounter = 0;

  function hasFiles(event) {
    return Boolean(event.dataTransfer) && Array.from(event.dataTransfer.types || []).includes("Files");
  }

  document.addEventListener("dragenter", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    dragCounter += 1;
    if (dropZone) dropZone.classList.add("drag-over");
  });

  document.addEventListener("dragover", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
  });

  document.addEventListener("dragleave", (event) => {
    if (!hasFiles(event)) return;
    dragCounter = Math.max(0, dragCounter - 1);
    if (dragCounter === 0 && dropZone) dropZone.classList.remove("drag-over");
  });

  document.addEventListener("drop", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    dragCounter = 0;
    if (dropZone) dropZone.classList.remove("drag-over");
    const files = Array.from(event.dataTransfer.files || []);
    if (files.length === 0) return;
    const text = textarea ? textarea.value : "";
    handleSubmit(text, files);
  });

  // ---------- Initialization ----------
  (async function init() {
    await loadStages();
    await loadDeal();
  })();
})();
