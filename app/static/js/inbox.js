// Inbox page logic: quick note dropping, the "Inbox" feed, keyboard triage
// mode, bulk attach and the recovery banner.
// All network requests are same-origin /api/... only, via window.OpsCenter.apiFetch.
"use strict";

(function () {
  // ---------- Pure pendingFiles buffer helpers ----------
  // No DOM, they operate on a plain array and return a new one. Exposed on
  // window.OpsCenter to be covered by a Node/assert test (see tests/js/test_pending_files.mjs).
  function addPendingFile(files, file) {
    return files.concat([file]);
  }
  function removePendingFileAt(files, index) {
    const next = files.slice();
    next.splice(index, 1);
    return next;
  }
  if (window.OpsCenter) {
    window.OpsCenter.addPendingFile = addPendingFile;
    window.OpsCenter.removePendingFileAt = removePendingFileAt;
  }

  const page = document.querySelector('[data-page="inbox"]');
  if (!page) return;

  const textarea = page.querySelector("[data-quick-drop]");
  const dropZone = page.querySelector("[data-drop-zone]");
  const errorEl = page.querySelector("[data-drop-error]");
  const feedEl = page.querySelector("[data-note-feed]");
  const filterButtons = Array.from(page.querySelectorAll("[data-filter]"));

  const bulkToolbar = page.querySelector("[data-bulk-toolbar]");
  const bulkCountEl = page.querySelector("[data-bulk-count]");
  const bulkAttachBtn = page.querySelector("[data-bulk-attach]");

  const recoveryBanner = page.querySelector("[data-recovery-banner]");
  const recoveryTextEl = page.querySelector("[data-recovery-text]");
  const recoveryScanBtn = page.querySelector("[data-recovery-scan]");
  const recoveryDeferBtn = page.querySelector("[data-recovery-defer]");

  const attachDropdown = page.querySelector("[data-attach-dropdown]");
  const attachSearchInput = page.querySelector("[data-attach-search]");
  const attachResultsEl = page.querySelector("[data-attach-results]");

  // "Parse all" panel: a sequential pass over unparsed inbox notes.
  const parseAllPanel = page.querySelector("[data-parse-all-panel]");
  const parseAllToggle = page.querySelector("[data-parse-all-toggle]");
  const parseAllBody = page.querySelector("[data-parse-all-body]");
  const parseAllChevron = page.querySelector("[data-parse-all-chevron]");
  const parseAllStartBtn = page.querySelector("[data-parse-all-start]");
  const parseAllStopBtn = page.querySelector("[data-parse-all-stop]");
  const parseAllProgress = page.querySelector("[data-parse-all-progress]");

  let currentFilter = "inbox";
  // Filter by note type, toggled by clicking the type badge. null = off.
  let activeTypeFilter = null;
  let notes = [];

  // ---------- Attachment buffer ----------
  // Dropped/pasted files accumulate here and are sent only on Enter together
  // with the text; the preview with [x] under the text field lets you drop an
  // unwanted file.
  let pendingFiles = [];
  let pendingPreviewEl = null;

  // ---------- LLM parse state ----------
  // parsingIds -- notes for which a /parse request is in flight (card spinner);
  // parseErrors -- id -> last /parse error text (banner with a "Retry" button);
  // parseSkipped -- id -> number of skipped images from the /parse response (not stored in DB).
  const parsingIds = new Set();
  const parseErrors = new Map();
  const parseSkipped = new Map();
  // Confidence threshold (GET /api/settings/parse); null until loaded / on error.
  let confidenceThreshold = null;
  // Cache of item id -> title (for the "-> <Item>" banner); same source as the dropdown.
  const dealTitleCache = new Map();
  let dealTitlesLoaded = false;
  // "Parse all" run state.
  let bulkParseRunning = false;
  let bulkParseStop = false;

  // Note type labels -- shared source in common.js (includes "info").
  const NOTE_TYPE_LABELS = window.OpsCenter.NOTE_TYPE_LABELS;
  const PARSE_ALL_COLLAPSE_KEY = "opscenter.parseAllPanelCollapsed";

  // ---------- Triage state ----------
  let selectedNoteId = null;
  const checkedIds = new Set();
  let quickScanActive = false;

  // Attach dropdown state (used both for single attach via "s"/button and for
  // bulk-attaching several selected notes).
  let attachTarget = null; // { kind: 'single', noteId } | { kind: 'bulk' }
  let attachResults = [];
  let attachHighlight = 0;
  let attachSearchTimer = null;

  const showError = window.OpsCenter.makeErrorShower(errorEl);

  function focusDrop() {
    if (!textarea) return;
    textarea.focus();
  }

  function isEditableTarget(el) {
    if (!el) return false;
    if (el.isContentEditable) return true;
    const tag = el.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
  }

  function getVisibleNotes() {
    // The "inbox" and "info" tabs share one server-side status=inbox split by
    // note_type: "inbox" -- everything except info; "info" -- only info. Other
    // tabs (deferred/archived) are not split. The type-badge filter is applied
    // on top.
    let visible = notes;
    if (currentFilter === "inbox") {
      visible = visible.filter((n) => n.note_type !== "info");
    } else if (currentFilter === "info") {
      visible = visible.filter((n) => n.note_type === "info");
    }
    if (activeTypeFilter != null) {
      visible = visible.filter((n) => n.note_type === activeTypeFilter);
    }
    return quickScanActive ? visible.slice(0, 15) : visible;
  }

  async function loadFeed(status) {
    // Loads notes by status. The "info" tab pulls the same server-side
    // status=inbox; the inbox/info split is done client-side in getVisibleNotes.
    const apiStatus = status === "info" ? "inbox" : status;
    try {
      notes = await window.OpsCenter.apiFetch(`/api/notes?status=${encodeURIComponent(apiStatus)}`);
    } catch (err) {
      notes = [];
      showError(err.message || "Failed to load the feed");
    }
    quickScanActive = false;
    checkedIds.clear();
    // A fresh feed load resets transient parse state (spinners/errors/skipped
    // image counters belong to the previous cards).
    parsingIds.clear();
    parseErrors.clear();
    parseSkipped.clear();
    // Keep the selection if the note is still in the feed, otherwise reset it.
    if (!notes.some((n) => n.id === selectedNoteId)) {
      selectedNoteId = null;
    }
    // If the feed has suggestions -- pull item titles for the banners.
    if (notes.some((n) => n.llm_status === "suggested" && n.suggested_deal_id != null)) {
      await ensureDealTitles();
    }
    renderFeed();
    updateBulkToolbar();
    if (status === "inbox") {
      refreshRecoveryBanner();
    } else {
      hideRecoveryBanner();
    }
  }

  // The buffer preview container is created lazily and inserted right below the drop zone.
  function ensurePendingPreviewEl() {
    if (pendingPreviewEl) return pendingPreviewEl;
    pendingPreviewEl = document.createElement("div");
    pendingPreviewEl.className = "pending-files";
    if (dropZone && dropZone.parentNode) {
      dropZone.parentNode.insertBefore(pendingPreviewEl, dropZone.nextSibling);
    }
    return pendingPreviewEl;
  }

  function renderPendingFiles() {
    const el = ensurePendingPreviewEl();
    el.innerHTML = "";
    pendingFiles.forEach((file, index) => {
      const row = document.createElement("div");
      row.className = "pending-file";
      const name = document.createElement("span");
      name.className = "pending-file-name";
      name.textContent = file.name;
      const size = document.createElement("span");
      size.className = "pending-file-size";
      size.textContent = window.OpsCenter.humanFileSize(file.size);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "pending-file-remove";
      remove.textContent = "×";
      remove.setAttribute("aria-label", "Remove file");
      remove.addEventListener("click", () => {
        pendingFiles = removePendingFileAt(pendingFiles, index);
        renderPendingFiles();
      });
      row.appendChild(name);
      row.appendChild(size);
      row.appendChild(remove);
      el.appendChild(row);
    });
  }

  function bufferFiles(files) {
    for (const file of files) pendingFiles = addPendingFile(pendingFiles, file);
    renderPendingFiles();
  }

  // ---------- Triage actions on a single note ----------

  async function reloadAfterAction() {
    await loadFeed(currentFilter);
    window.OpsCenter.refreshInboxCounter();
  }

  // Attaches a single note to an item (the "To item" button action, or "s" on the keyboard).
  async function attachNoteToDeal(noteId, dealId) {
    try {
      await window.OpsCenter.apiFetch(`/api/notes/${noteId}`, {
        method: "PATCH",
        body: { deal_id: dealId },
      });
    } catch (err) {
      showError(err.message || "Failed to attach the note");
      return;
    }
    await reloadAfterAction();
  }

  // Attaches several selected notes to one item at once (bulk mode).
  async function bulkAttachNotes(noteIds, dealId) {
    try {
      await window.OpsCenter.apiFetch("/api/notes/bulk-attach", {
        method: "POST",
        body: { note_ids: noteIds, deal_id: dealId },
      });
    } catch (err) {
      showError(err.message || "Failed to attach the notes");
      return;
    }
    checkedIds.clear();
    await reloadAfterAction();
  }

  // Moves a note to the archive (the "Archive" button action, or "a" on the keyboard).
  async function archiveNote(noteId) {
    try {
      await window.OpsCenter.apiFetch(`/api/notes/${noteId}`, {
        method: "PATCH",
        body: { status: "archived" },
      });
    } catch (err) {
      showError(err.message || "Failed to archive the note");
      return;
    }
    await reloadAfterAction();
  }

  async function setNoteType(noteId, noteType) {
    try {
      await window.OpsCenter.apiFetch(`/api/notes/${noteId}`, {
        method: "PATCH",
        body: { note_type: noteType },
      });
    } catch (err) {
      showError(err.message || "Failed to change the note type");
      return;
    }
    await reloadAfterAction();
  }

  // Pins/unpins a note. "Pinned on top" ordering is computed by the server, so
  // we just reload the feed -- no client-side sorting.
  async function setNotePinned(noteId, pinned) {
    try {
      await window.OpsCenter.apiFetch(`/api/notes/${noteId}`, {
        method: "PATCH",
        body: { is_pinned: pinned },
      });
    } catch (err) {
      showError(err.message || "Failed to pin the note");
      return;
    }
    await reloadAfterAction();
  }

  // Deletes a note permanently (the "Delete" button action, or "d" on the keyboard).
  async function deleteNote(noteId) {
    if (!window.confirm("Delete this note permanently?")) return;
    try {
      await window.OpsCenter.apiFetch(`/api/notes/${noteId}`, { method: "DELETE" });
    } catch (err) {
      showError(err.message || "Failed to delete the note");
      return;
    }
    await reloadAfterAction();
  }

  // ---------- Attach-to-item dropdown (keyboard "s" + mouse) ----------

  function closeAttachDropdown() {
    if (!attachDropdown) return;
    attachDropdown.hidden = true;
    attachTarget = null;
    attachResults = [];
    attachHighlight = 0;
    if (attachSearchInput) attachSearchInput.value = "";
    if (attachResultsEl) attachResultsEl.innerHTML = "";
  }

  function isAttachDropdownOpen() {
    return Boolean(attachDropdown) && !attachDropdown.hidden;
  }

  function renderAttachResults() {
    if (!attachResultsEl) return;
    attachResultsEl.innerHTML = "";
    if (attachResults.length === 0) {
      const empty = document.createElement("div");
      empty.className = "attach-dropdown-empty";
      empty.textContent = "Nothing found";
      attachResultsEl.appendChild(empty);
      return;
    }
    attachResults.forEach((deal, index) => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "attach-dropdown-result";
      if (index === attachHighlight) item.classList.add("highlighted");
      item.textContent = deal.title;
      item.addEventListener("click", () => selectAttachResult(index));
      attachResultsEl.appendChild(item);
    });
  }

  async function runAttachSearch(query) {
    try {
      attachResults = await window.OpsCenter.apiFetch(`/api/deals?q=${encodeURIComponent(query)}`);
    } catch (_err) {
      attachResults = [];
    }
    attachHighlight = 0;
    renderAttachResults();
  }

  function selectAttachResult(index) {
    const deal = attachResults[index];
    if (!deal || !attachTarget) return;
    const target = attachTarget;
    closeAttachDropdown();
    if (target.kind === "bulk") {
      bulkAttachNotes(Array.from(checkedIds), deal.id);
    } else if (target.kind === "change") {
      changeNoteDeal(target.noteId, deal.id);
    } else {
      attachNoteToDeal(target.noteId, deal.id);
    }
  }

  function positionAttachDropdown(anchorEl) {
    if (!attachDropdown || !anchorEl) return;
    const rect = anchorEl.getBoundingClientRect();
    attachDropdown.style.position = "fixed";
    attachDropdown.style.top = `${Math.min(rect.bottom + 4, window.innerHeight - 60)}px`;
    attachDropdown.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - 280))}px`;
  }

  // Opens the attach-to-item dropdown (used both for single attach and bulk
  // mode). After opening, focus goes to the search field.
  function openAttachDropdown(anchorEl, target) {
    if (!attachDropdown) return;
    attachTarget = target;
    positionAttachDropdown(anchorEl);
    attachDropdown.hidden = false;
    if (attachSearchInput) {
      attachSearchInput.value = "";
      attachSearchInput.focus();
    }
    runAttachSearch("");
  }

  if (attachSearchInput) {
    attachSearchInput.addEventListener("input", () => {
      if (attachSearchTimer) window.clearTimeout(attachSearchTimer);
      const query = attachSearchInput.value;
      attachSearchTimer = window.setTimeout(() => runAttachSearch(query), 200);
    });

    attachSearchInput.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeAttachDropdown();
        return;
      }
      if (event.key === "ArrowDown") {
        event.preventDefault();
        if (attachResults.length) {
          attachHighlight = Math.min(attachResults.length - 1, attachHighlight + 1);
          renderAttachResults();
        }
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        if (attachResults.length) {
          attachHighlight = Math.max(0, attachHighlight - 1);
          renderAttachResults();
        }
        return;
      }
      if (event.key === "Enter") {
        event.preventDefault();
        if (attachResults.length) selectAttachResult(attachHighlight);
      }
    });
  }

  document.addEventListener("click", (event) => {
    if (!isAttachDropdownOpen()) return;
    if (attachDropdown.contains(event.target)) return;
    closeAttachDropdown();
  });

  // ---------- Bulk panel ----------

  function updateBulkToolbar() {
    if (!bulkToolbar) return;
    const count = checkedIds.size;
    bulkToolbar.hidden = count === 0;
    if (bulkCountEl) bulkCountEl.textContent = `Selected: ${count}`;
  }

  if (bulkAttachBtn) {
    bulkAttachBtn.addEventListener("click", (event) => {
      // stopPropagation: prevents the dropdown from being closed by the document-level click-outside handler
      // (if the event reached document, the dropdown would close the very moment we open it).
      event.stopPropagation();
      if (checkedIds.size === 0) return;
      openAttachDropdown(bulkAttachBtn, { kind: "bulk" });
    });
  }

  // ---------- Recovery banner ----------
  // Shows a banner when the Inbox is overloaded (recovery_needed=true from /api/inbox/summary).
  // "Quick scan" shows the 15 newest notes (quickScanActive=true),
  // "Defer the rest" moves older ones to the deferred status via /api/notes/defer-old.

  async function refreshRecoveryBanner() {
    if (!recoveryBanner) return;
    let summary;
    try {
      summary = await window.OpsCenter.apiFetch("/api/inbox/summary");
    } catch (_err) {
      hideRecoveryBanner();
      return;
    }
    if (summary && summary.recovery_needed) {
      recoveryBanner.hidden = false;
      if (recoveryTextEl) {
        recoveryTextEl.textContent = `The Inbox has piled up (${summary.inbox_count}) — time to triage the Inbox.`;
      }
    } else {
      hideRecoveryBanner();
    }
  }

  function hideRecoveryBanner() {
    if (recoveryBanner) recoveryBanner.hidden = true;
    quickScanActive = false;
  }

  if (recoveryScanBtn) {
    recoveryScanBtn.addEventListener("click", () => {
      quickScanActive = true;
      renderFeed();
    });
  }

  if (recoveryDeferBtn) {
    recoveryDeferBtn.addEventListener("click", async () => {
      try {
        await window.OpsCenter.apiFetch("/api/notes/defer-old", {
          method: "POST",
          body: { keep: 15 },
        });
      } catch (err) {
        showError(err.message || "Failed to defer the old notes");
        return;
      }
      quickScanActive = false;
      await reloadAfterAction();
    });
  }

  // ---------- LLM note parsing ----------

  // ensureDealTitles: pulls active items once (same source as the attach
  // dropdown) and caches id -> title for suggestion banners.
  async function ensureDealTitles() {
    if (dealTitlesLoaded) return;
    try {
      const deals = await window.OpsCenter.apiFetch("/api/deals?q=");
      for (const deal of deals) {
        dealTitleCache.set(deal.id, deal.title);
      }
      dealTitlesLoaded = true;
    } catch (_err) {
      // Failed to fetch titles -- the banner will show the fallback "Item #id".
    }
  }

  function dealTitle(dealId) {
    return dealTitleCache.get(dealId) || `Item #${dealId}`;
  }

  // parseNote: starts parsing a single note. The spinner is placed ONLY on that
  // card -- the rest of the feed stays usable (the UI is not blocked). On
  // success it updates the note object with llm fields and shows the suggestion
  // banner; on a network/API error -- a banner with the error text and a
  // "Retry" button, the note is not lost.
  async function parseNote(noteId) {
    parsingIds.add(noteId);
    parseErrors.delete(noteId);
    renderFeed();

    let result;
    try {
      result = await window.OpsCenter.apiFetch(`/api/notes/${noteId}/parse`, { method: "POST" });
    } catch (err) {
      parsingIds.delete(noteId);
      parseErrors.set(noteId, err.message || "Failed to parse the note");
      renderFeed();
      return;
    }

    parsingIds.delete(noteId);
    if (typeof result.skipped_images === "number" && result.skipped_images > 0) {
      parseSkipped.set(noteId, result.skipped_images);
    } else {
      parseSkipped.delete(noteId);
    }
    const idx = notes.findIndex((n) => n.id === noteId);
    if (idx !== -1) {
      notes[idx] = Object.assign({}, notes[idx], result);
    }
    if (result.suggested_deal_id != null) {
      await ensureDealTitles();
    }
    renderFeed();
  }

  // confirmNote: confirms the suggestion -> the note is attached to the
  // suggested item and leaves the Inbox; the inbox counter is refreshed via
  // reloadAfterAction.
  async function confirmNote(noteId) {
    try {
      await window.OpsCenter.apiFetch(`/api/notes/${noteId}/confirm`, { method: "POST" });
    } catch (err) {
      showError(err.message || "Failed to confirm the suggestion");
      return;
    }
    await reloadAfterAction();
  }

  // changeNoteDeal: the user picked an item themselves (via the dropdown) ->
  // POST /change, llm_status='rejected', the LLM suggestion is kept in the data.
  async function changeNoteDeal(noteId, dealId) {
    try {
      await window.OpsCenter.apiFetch(`/api/notes/${noteId}/change`, {
        method: "POST",
        body: { deal_id: dealId },
      });
    } catch (err) {
      showError(err.message || "Failed to change the attachment");
      return;
    }
    await reloadAfterAction();
  }

  // rejectNote: rejects the suggestion -> the banner is removed, the note stays
  // in the feed unattached (llm_status='rejected', status='inbox').
  async function rejectNote(noteId) {
    try {
      await window.OpsCenter.apiFetch(`/api/notes/${noteId}/reject`, { method: "POST" });
    } catch (err) {
      showError(err.message || "Failed to reject the suggestion");
      return;
    }
    await reloadAfterAction();
  }

  // buildSpinner: a loading indicator, built from scratch, palette from the
  // existing CSS variables.
  function buildSpinner(labelText) {
    const wrap = document.createElement("div");
    wrap.className = "note-parse-spinner";
    const spinner = document.createElement("span");
    spinner.className = "spinner";
    spinner.setAttribute("aria-hidden", "true");
    wrap.appendChild(spinner);
    const label = document.createElement("span");
    label.textContent = labelText || "Parsing…";
    wrap.appendChild(label);
    return wrap;
  }

  // buildParseErrorBanner: a parse-error banner with a "Retry" button.
  function buildParseErrorBanner(note) {
    const banner = document.createElement("div");
    banner.className = "parse-banner parse-banner--error";

    const text = document.createElement("p");
    text.className = "parse-banner-error-text";
    text.textContent = parseErrors.get(note.id) || "Failed to parse the note";
    banner.appendChild(text);

    const actions = document.createElement("div");
    actions.className = "parse-banner-actions";
    const retryBtn = document.createElement("button");
    retryBtn.type = "button";
    retryBtn.className = "small";
    retryBtn.textContent = "Retry";
    retryBtn.addEventListener("click", () => {
      selectedNoteId = note.id;
      parseNote(note.id);
    });
    actions.appendChild(retryBtn);
    banner.appendChild(actions);
    return banner;
  }

  // buildSuggestionBanner: the LLM suggestion banner.
  // "-> <Item> · <type> · confidence 0.92" + draft + Confirm/Change/Reject.
  // Low confidence (< threshold) -> muted look + "low confidence" label.
  // suggested_deal_id=null -> "item not determined", no "Confirm".
  function buildSuggestionBanner(note) {
    const banner = document.createElement("div");
    banner.className = "parse-banner";

    const hasDeal = note.suggested_deal_id != null;
    const conf = typeof note.llm_confidence === "number" ? note.llm_confidence : null;
    const isLow = conf != null && confidenceThreshold != null && conf < confidenceThreshold;
    if (!hasDeal) banner.classList.add("parse-banner--nodeal");
    if (isLow) banner.classList.add("parse-banner--low");

    // Suggestion headline (all data-derived strings go through textContent).
    const headline = document.createElement("div");
    headline.className = "parse-banner-headline";
    if (hasDeal) {
      headline.appendChild(document.createTextNode(`→ ${dealTitle(note.suggested_deal_id)}`));
    } else {
      headline.appendChild(document.createTextNode("item not determined"));
    }
    const typeLabel = note.suggested_note_type ? NOTE_TYPE_LABELS[note.suggested_note_type] : null;
    if (typeLabel) {
      headline.appendChild(document.createTextNode(` · ${typeLabel}`));
    }
    if (conf != null) {
      headline.appendChild(document.createTextNode(` · confidence ${conf.toFixed(2)}`));
    }
    banner.appendChild(headline);

    if (isLow) {
      const lowLabel = document.createElement("span");
      lowLabel.className = "parse-banner-label";
      lowLabel.textContent = "low confidence";
      banner.appendChild(lowLabel);
    }

    if (note.llm_draft) {
      const draft = document.createElement("p");
      draft.className = "parse-banner-draft";
      draft.textContent = note.llm_draft;
      banner.appendChild(draft);
    }

    const skipped = parseSkipped.get(note.id);
    if (skipped) {
      const skippedEl = document.createElement("div");
      skippedEl.className = "parse-banner-skipped";
      skippedEl.textContent = `${skipped} images skipped`;
      banner.appendChild(skippedEl);
    }

    const actions = document.createElement("div");
    actions.className = "parse-banner-actions";

    if (hasDeal) {
      const confirmBtn = document.createElement("button");
      confirmBtn.type = "button";
      confirmBtn.className = "small primary";
      confirmBtn.textContent = "Confirm";
      confirmBtn.addEventListener("click", () => {
        selectedNoteId = note.id;
        confirmNote(note.id);
      });
      actions.appendChild(confirmBtn);
    }

    const changeBtn = document.createElement("button");
    changeBtn.type = "button";
    changeBtn.className = "small";
    changeBtn.textContent = "Change";
    changeBtn.addEventListener("click", (event) => {
      // stopPropagation: prevents the document-level click-outside from closing the dropdown immediately.
      event.stopPropagation();
      selectedNoteId = note.id;
      openAttachDropdown(changeBtn, { kind: "change", noteId: note.id });
    });
    actions.appendChild(changeBtn);

    const rejectBtn = document.createElement("button");
    rejectBtn.type = "button";
    rejectBtn.className = "small danger";
    rejectBtn.textContent = "Reject";
    rejectBtn.addEventListener("click", () => {
      selectedNoteId = note.id;
      rejectNote(note.id);
    });
    actions.appendChild(rejectBtn);

    banner.appendChild(actions);
    return banner;
  }

  // buildParseArea: the part of the card below the actions -- spinner / error /
  // suggestion banner depending on the note's current parse state.
  function buildParseArea(note) {
    if (parsingIds.has(note.id)) return buildSpinner();
    if (parseErrors.has(note.id)) return buildParseErrorBanner(note);
    if (note.llm_status === "suggested") return buildSuggestionBanner(note);
    return null;
  }

  // ---------- Note card rendering ----------

  function renderNoteCard(note) {
    const card = document.createElement("article");
    card.className = "note-card";
    if (note.id === selectedNoteId) card.classList.add("selected");
    if (note.is_pinned) card.classList.add("pinned");
    card.dataset.noteId = String(note.id);

    const topRow = document.createElement("div");
    topRow.className = "note-top-row";

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "note-checkbox";
    checkbox.checked = checkedIds.has(note.id);
    checkbox.setAttribute("aria-label", "Select note");
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) checkedIds.add(note.id);
      else checkedIds.delete(note.id);
      updateBulkToolbar();
    });
    topRow.appendChild(checkbox);

    const body = document.createElement("div");
    body.className = "note-card-main";

    if (note.body) {
      const bodyEl = document.createElement("p");
      bodyEl.className = "note-body";
      // Escape + linkify links. innerHTML is safe only because linkifyEscaped
      // escapes the text BEFORE inserting <a>.
      bodyEl.innerHTML = window.OpsCenter.linkifyEscaped(note.body);
      body.appendChild(bodyEl);
    }

    if (note.attachments && note.attachments.length) {
      const attWrap = document.createElement("div");
      attWrap.className = "note-attachments";
      for (const att of note.attachments) {
        attWrap.appendChild(window.OpsCenter.renderAttachment(att));
      }
      body.appendChild(attWrap);
    }

    // OCR text: LLM output, rendered as plain text (textContent), not
    // linkified and not rendered as HTML (stored-XSS risk).
    if (note.ocr_text) {
      const ocr = document.createElement("div");
      ocr.className = "note-ocr-text";
      ocr.textContent = note.ocr_text;
      body.appendChild(ocr);
    }

    const meta = document.createElement("div");
    meta.className = "note-meta";
    const time = document.createElement("span");
    time.className = "note-time";
    time.textContent = window.formatDate ? window.formatDate(note.created_at) : note.created_at;
    meta.appendChild(time);
    if (note.note_type) {
      const type = document.createElement("span");
      type.className = "note-type-badge";
      type.textContent = NOTE_TYPE_LABELS[note.note_type] || note.note_type;
      // Clicking the badge -> filter the feed by this type.
      type.setAttribute("role", "button");
      type.title = "Filter by type";
      type.addEventListener("click", (event) => {
        event.stopPropagation();
        activeTypeFilter = note.note_type;
        renderFeed();
      });
      meta.appendChild(type);
    }
    body.appendChild(meta);

    topRow.appendChild(body);
    card.appendChild(topRow);

    // ---------- One-click actions ----------
    const actions = document.createElement("div");
    actions.className = "note-actions";

    // "Parse" -- only for an unparsed note in the Inbox feed (llm_status='none').
    if (currentFilter === "inbox" && note.llm_status === "none" && !parsingIds.has(note.id)) {
      const parseBtn = document.createElement("button");
      parseBtn.type = "button";
      parseBtn.className = "small";
      parseBtn.textContent = "Parse";
      parseBtn.addEventListener("click", () => {
        selectedNoteId = note.id;
        parseNote(note.id);
      });
      actions.appendChild(parseBtn);
    }

    const attachBtn = document.createElement("button");
    attachBtn.type = "button";
    attachBtn.className = "small";
    attachBtn.textContent = "To item";
    attachBtn.addEventListener("click", (event) => {
      // stopPropagation: prevents the dropdown from being closed by the document-level click-outside handler.
      event.stopPropagation();
      selectedNoteId = note.id;
      openAttachDropdown(attachBtn, { kind: "single", noteId: note.id });
    });
    actions.appendChild(attachBtn);

    const taskBtn = document.createElement("button");
    taskBtn.type = "button";
    taskBtn.className = "small";
    if (note.note_type === "task") taskBtn.classList.add("active-toggle");
    taskBtn.textContent = "Task";
    taskBtn.addEventListener("click", () => {
      selectedNoteId = note.id;
      setNoteType(note.id, note.note_type === "task" ? null : "task");
    });
    actions.appendChild(taskBtn);

    const reminderBtn = document.createElement("button");
    reminderBtn.type = "button";
    reminderBtn.className = "small";
    if (note.note_type === "reminder") reminderBtn.classList.add("active-toggle");
    reminderBtn.textContent = "Reminder";
    reminderBtn.addEventListener("click", () => {
      selectedNoteId = note.id;
      setNoteType(note.id, note.note_type === "reminder" ? null : "reminder");
    });
    actions.appendChild(reminderBtn);

    const infoBtn = document.createElement("button");
    infoBtn.type = "button";
    infoBtn.className = "small";
    if (note.note_type === "info") infoBtn.classList.add("active-toggle");
    infoBtn.textContent = "Info";
    infoBtn.addEventListener("click", () => {
      selectedNoteId = note.id;
      setNoteType(note.id, note.note_type === "info" ? null : "info");
    });
    actions.appendChild(infoBtn);

    const pinBtn = document.createElement("button");
    pinBtn.type = "button";
    pinBtn.className = "small";
    if (note.is_pinned) pinBtn.classList.add("active-toggle");
    pinBtn.textContent = note.is_pinned ? "Unpin" : "Pin";
    pinBtn.addEventListener("click", () => {
      selectedNoteId = note.id;
      setNotePinned(note.id, !note.is_pinned);
    });
    actions.appendChild(pinBtn);

    const archiveBtn = document.createElement("button");
    archiveBtn.type = "button";
    archiveBtn.className = "small";
    archiveBtn.textContent = "Archive";
    archiveBtn.addEventListener("click", () => {
      selectedNoteId = note.id;
      archiveNote(note.id);
    });
    actions.appendChild(archiveBtn);

    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.className = "small danger";
    deleteBtn.textContent = "Delete";
    deleteBtn.addEventListener("click", () => {
      selectedNoteId = note.id;
      deleteNote(note.id);
    });
    actions.appendChild(deleteBtn);

    card.appendChild(actions);

    const parseArea = buildParseArea(note);
    if (parseArea) card.appendChild(parseArea);

    card.addEventListener("click", (event) => {
      // Clicking the card itself (not a button/checkbox/link) -- just selects it.
      if (event.target.closest("button, a, input")) return;
      selectedNoteId = note.id;
      renderFeed();
    });

    return card;
  }

  function scrollSelectedIntoView() {
    if (selectedNoteId == null) return;
    const el = feedEl.querySelector(`[data-note-id="${selectedNoteId}"]`);
    if (el) el.scrollIntoView({ block: "nearest" });
  }

  function renderTypeFilterPill() {
    // A dismissible "pill" for the active type filter.
    const pill = document.createElement("div");
    pill.className = "note-type-filter-pill";
    const label = document.createElement("span");
    label.textContent = `Type: ${NOTE_TYPE_LABELS[activeTypeFilter] || activeTypeFilter}`;
    pill.appendChild(label);
    const clear = document.createElement("button");
    clear.type = "button";
    clear.className = "note-type-filter-clear";
    clear.textContent = "×";
    clear.setAttribute("aria-label", "Clear type filter");
    clear.addEventListener("click", () => {
      activeTypeFilter = null;
      renderFeed();
    });
    pill.appendChild(clear);
    return pill;
  }

  function renderFeed() {
    // Renders the feed: clears the container, outputs notes as cards or an empty message.
    feedEl.innerHTML = "";
    if (activeTypeFilter != null) feedEl.appendChild(renderTypeFilterPill());
    const visible = getVisibleNotes();
    if (!visible || visible.length === 0) {
      const empty = document.createElement("p");
      empty.className = "page-hint note-feed-empty";
      empty.textContent = "Nothing here.";
      feedEl.appendChild(empty);
      return;
    }
    const frag = document.createDocumentFragment();
    for (const note of visible) {
      frag.appendChild(renderNoteCard(note));
    }
    feedEl.appendChild(frag);
  }

  async function submitNote(bodyText, files) {
    // Creates a note: sends text (optional) and files (optional).
    // Returns null if there is nothing (an empty drop -- no request).
    // Otherwise -- the created note object with attachments.
    const text = (bodyText || "").trim();
    const fileList = files || [];
    if (!text && fileList.length === 0) return null;

    const formData = new FormData();
    formData.append("body", bodyText || "");
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
      showError(err.message || "Failed to submit the note");
      return;
    }
    if (!note) return null; // empty drop -- there was no request

    if (textarea) textarea.value = "";

    if (currentFilter === "inbox" && note.status === "inbox") {
      notes.unshift(note);
      renderFeed();
    }

    window.OpsCenter.refreshInboxCounter();
    if (currentFilter === "inbox") refreshRecoveryBanner();
    focusDrop();
    return note;
  }

  function setFilter(filter) {
    currentFilter = filter;
    // Switching tabs resets the type-badge filter.
    activeTypeFilter = null;
    selectedNoteId = null;
    checkedIds.clear();
    for (const btn of filterButtons) {
      const active = btn.dataset.filter === filter;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-selected", active ? "true" : "false");
    }
    loadFeed(filter);
  }

  // ---------- Input: Enter submits, Shift+Enter -- newline ----------
  if (textarea) {
    textarea.addEventListener("keydown", async (event) => {
      if (event.key !== "Enter" || event.shiftKey) return;
      event.preventDefault();
      const text = textarea.value;
      // Enter submits the attachment buffer together with the text; an empty
      // Enter with no files -- no request.
      if (!text.trim() && pendingFiles.length === 0) return;
      const note = await handleSubmit(text, pendingFiles);
      if (note) {
        pendingFiles = [];
        renderPendingFiles();
      }
    });

    // ---------- Pasting an image/file from the clipboard (Ctrl+V) ----------
    // On Ctrl+V files accumulate in pendingFiles and previews are shown;
    // submission happens only on Enter. With no files -- the browser's normal text paste.
    textarea.addEventListener("paste", (event) => {
      const items = event.clipboardData ? Array.from(event.clipboardData.items) : [];
      const files = items
        .filter((item) => item.kind === "file")
        .map((item) => item.getAsFile())
        .filter(Boolean);
      if (files.length === 0) return; // normal text paste -- default behavior
      event.preventDefault();
      bufferFiles(files);
    });
  }

  // ---------- Drag-and-drop of files onto the page ----------
  // dragenter/dragover/dragleave -- control the visual highlight of the drop zone.
  // dragCounter tracks nested events (for paired enter/leave).
  // drop -- creates a note with the dropped files (and text from the field, if any).
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
    // Buffer instead of submitting: files are sent on Enter together with the text.
    bufferFiles(files);
  });

  // ---------- Feed filters ----------
  for (const btn of filterButtons) {
    btn.addEventListener("click", () => setFilter(btn.dataset.filter));
  }

  // ---------- Keyboard triage mode ----------
  // Up/Down select a note in the feed; "s" opens the attach dropdown; "d" deletes;
  // "a" archives. Does not fire when focus is in a text field/select/contenteditable
  // (including the quick-drop field and the attach dropdown search field) -- checked via
  // isEditableTarget(document.activeElement).

  // Moves the selection by delta positions (+-1 for arrows). If nothing is
  // selected, the first Down selects the first note, Up -- the last.
  function moveSelection(delta) {
    const visible = getVisibleNotes();
    if (!visible.length) return;
    let idx = visible.findIndex((n) => n.id === selectedNoteId);
    if (idx === -1) {
      // Nothing selected: for delta > 0 select the first (idx=0), otherwise the last (idx=len-1)
      idx = delta > 0 ? 0 : visible.length - 1;
    } else {
      // Move from the current position, staying within [0; len-1]
      idx = Math.min(visible.length - 1, Math.max(0, idx + delta));
    }
    selectedNoteId = visible[idx].id;
    renderFeed();
    scrollSelectedIntoView();
  }

  document.addEventListener("keydown", (event) => {
    if (isEditableTarget(document.activeElement)) return;
    if (isAttachDropdownOpen()) return;

    if (event.key === "ArrowDown") {
      event.preventDefault();
      moveSelection(1);
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      moveSelection(-1);
      return;
    }

    if (selectedNoteId == null) return; // s/d/a require a selected note

    // On a note with a suggestion: Enter = confirm, x = reject.
    // The base triage (Up/Down, s, d, a) is not affected.
    const selected = notes.find((n) => n.id === selectedNoteId);
    if (selected && selected.llm_status === "suggested") {
      if (event.key === "Enter") {
        event.preventDefault();
        if (selected.suggested_deal_id != null) confirmNote(selectedNoteId);
        return;
      }
      if (event.key === "x" || event.key === "X") {
        event.preventDefault();
        rejectNote(selectedNoteId);
        return;
      }
    }

    if (event.key === "s" || event.key === "S") {
      event.preventDefault();
      const card = feedEl.querySelector(`[data-note-id="${selectedNoteId}"]`);
      const anchor = card ? card.querySelector('[type="button"]') : feedEl;
      openAttachDropdown(anchor || feedEl, { kind: "single", noteId: selectedNoteId });
      return;
    }
    if (event.key === "d" || event.key === "D") {
      event.preventDefault();
      deleteNote(selectedNoteId);
      return;
    }
    if (event.key === "a" || event.key === "A") {
      event.preventDefault();
      archiveNote(selectedNoteId);
    }
  });

  // ---------- Confidence threshold setting (for muting the banner) ----------
  async function loadParseSettings() {
    try {
      const settings = await window.OpsCenter.apiFetch("/api/settings/parse");
      confidenceThreshold =
        typeof settings.confidence_threshold === "number" ? settings.confidence_threshold : null;
    } catch (_err) {
      confidenceThreshold = null; // unavailable -- do not mark banners "low confidence"
    }
  }

  // ---------- "Parse all" panel ----------
  // Collapsible (state in localStorage). A sequential pass over unparsed notes
  // (llm_status='none') with "i of N" progress and a "Stop" button (stops after
  // the current one). Notes that already have a suggestion are skipped
  // automatically (they are not selected).

  function setParseAllCollapsed(collapsed) {
    if (parseAllBody) parseAllBody.hidden = collapsed;
    if (parseAllToggle) parseAllToggle.setAttribute("aria-expanded", String(!collapsed));
    if (parseAllChevron) parseAllChevron.textContent = collapsed ? "▸" : "▾";
    try {
      window.localStorage.setItem(PARSE_ALL_COLLAPSE_KEY, collapsed ? "1" : "0");
    } catch (_err) {
      /* localStorage unavailable -- the state won't survive a reload */
    }
  }

  function setParseAllProgress(text) {
    if (parseAllProgress) parseAllProgress.textContent = text;
  }

  function updateParseAllControls() {
    if (parseAllStartBtn) parseAllStartBtn.hidden = bulkParseRunning;
    if (parseAllStopBtn) parseAllStopBtn.hidden = !bulkParseRunning;
  }

  async function runParseAll() {
    if (bulkParseRunning) return;
    const targets = notes.filter((n) => n.llm_status === "none");
    const total = targets.length;
    if (total === 0) {
      setParseAllProgress("No unparsed notes.");
      return;
    }
    bulkParseRunning = true;
    bulkParseStop = false;
    updateParseAllControls();
    let done = 0;
    for (const target of targets) {
      if (bulkParseStop) break;
      setParseAllProgress(`${done + 1} of ${total}`);
      // parseNote handles the spinner/banner/error on the card itself and does
      // not throw -- one note's error doesn't interrupt the run.
      await parseNote(target.id);
      done += 1;
    }
    bulkParseRunning = false;
    updateParseAllControls();
    setParseAllProgress(
      bulkParseStop ? `Stopped (${done} of ${total})` : `Done (${done} of ${total})`
    );
  }

  function initParseAllPanel() {
    if (!parseAllPanel) return;
    let collapsedInitial = true;
    try {
      collapsedInitial = window.localStorage.getItem(PARSE_ALL_COLLAPSE_KEY) !== "0";
    } catch (_err) {
      collapsedInitial = true;
    }
    setParseAllCollapsed(collapsedInitial);
    updateParseAllControls();

    if (parseAllToggle) {
      parseAllToggle.addEventListener("click", () => {
        setParseAllCollapsed(!(parseAllBody && parseAllBody.hidden));
      });
    }
    if (parseAllStartBtn) {
      parseAllStartBtn.addEventListener("click", () => runParseAll());
    }
    if (parseAllStopBtn) {
      parseAllStopBtn.addEventListener("click", () => {
        if (bulkParseRunning) {
          bulkParseStop = true;
          setParseAllProgress("Stopping after the current one…");
        }
      });
    }
  }

  // ---------- Initialization ----------
  focusDrop();
  initParseAllPanel();
  loadParseSettings();
  loadFeed(currentFilter);
})();
