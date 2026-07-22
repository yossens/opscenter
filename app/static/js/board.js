// "Board" and "Archive" page logic: renders columns from GET /api/board,
// HTML5 drag-and-drop of cards between columns (POST /api/deals/{id}/move),
// a collapsed terminal "Done" column linking to /archive,
// the "+ Item" modal (POST /api/deals) and the list of closed items on /archive.
// All network requests are same-origin /api/... only, via window.OpsCenter.apiFetch.
"use strict";

(function () {
  function badgeClassForAging(level) {
    if (level === "warn") return "aging-warn";
    if (level === "overdue") return "aging-overdue";
    return "aging-ok";
  }

  // formatShortDate: "snoozed until DD.MM" on the board card. card.snoozed_until
  // is a local calendar date "YYYY-MM-DD" (not a UTC moment in time), so it must
  // not go through window.formatDate -- that treats a string without a TZ suffix
  // as UTC midnight, and converting to the browser's local TZ could shift the
  // day. We parse the date components directly, without Date/TZ.
  function formatShortDate(dateStr) {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dateStr || "");
    if (!match) return "";
    return `${match[3]}.${match[2]}`;
  }

  // reloadBoardRef: reference to loadBoard() from initBoardPage(), set by that
  // same call below. The "Ping Today" panel uses it after a snooze to refresh
  // the "snoozed until..." badge on the board card without a full page reload --
  // a second re-fetch (board) besides its own re-fetch of /api/pings.
  let reloadBoardRef = null;

  function initBoardPage() {
    const page = document.querySelector('[data-page="board"]');
    if (!page) return;

    const columnsEl = page.querySelector("[data-board-columns]");
    const emptyEl = page.querySelector("[data-board-empty]");
    const newDealBtn = page.querySelector("[data-new-deal]");
    const modal = document.querySelector("[data-new-deal-modal]");
    const form = modal ? modal.querySelector("[data-new-deal-form]") : null;
    const errorEl = modal ? modal.querySelector("[data-new-deal-error]") : null;
    const closeBtn = modal ? modal.querySelector("[data-modal-close]") : null;
    const cancelBtn = modal ? modal.querySelector("[data-modal-cancel]") : null;

    // Vertical mouse wheel -> horizontal board scroll, but only when there is
    // real horizontal overflow. Otherwise we leave the event alone so as not to
    // break the page's normal vertical scroll (the board fits on screen).
    if (columnsEl) {
      columnsEl.addEventListener(
        "wheel",
        (e) => {
          if (e.deltaY === 0) return;
          if (columnsEl.scrollWidth <= columnsEl.clientWidth) return;
          e.preventDefault();
          columnsEl.scrollLeft += e.deltaY;
        },
        { passive: false }
      );
    }

    // The only state needed for DnD: id of the item currently being dragged.
    let draggedDealId = null;

    function renderCard(card) {
      const el = document.createElement("div");
      el.className = "board-card";
      el.draggable = true;
      el.dataset.dealId = String(card.id);

      const title = document.createElement("div");
      title.className = "board-card-title";
      title.textContent = card.title;
      el.appendChild(title);

      const subParts = [card.company, card.partner].filter(Boolean);
      if (subParts.length) {
        const sub = document.createElement("div");
        sub.className = "board-card-sub";
        sub.textContent = subParts.join(" · ");
        el.appendChild(sub);
      }

      const badges = document.createElement("div");
      badges.className = "board-card-badges";

      const daysBadge = document.createElement("span");
      daysBadge.className = `badge ${badgeClassForAging(card.aging_level)}`;
      daysBadge.textContent = `${card.days_in_stage} business days`;
      badges.appendChild(daysBadge);

      // Snooze badge: card.snoozed_until is a "YYYY-MM-DD" date if a snooze is
      // active strictly after today (backend), otherwise null -- the badge is
      // simply not drawn, and the item stays in its column.
      if (card.snoozed_until) {
        const snoozeBadge = document.createElement("span");
        snoozeBadge.className = "badge badge-snoozed";
        snoozeBadge.textContent = `snoozed until ${formatShortDate(card.snoozed_until)}`;
        badges.appendChild(snoozeBadge);
      }

      el.appendChild(badges);

      // Item delete button: confirm -> DELETE /api/deals/{id} -> full board
      // re-fetch. stopPropagation so clicking the cross does not open the card
      // (the click handler on el below).
      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "board-card-delete";
      delBtn.title = "Delete item";
      delBtn.textContent = "×";
      delBtn.addEventListener("click", async (event) => {
        event.stopPropagation();
        if (!window.confirm(`Delete item “${card.title}”? Its notes, attachments and pings will be permanently deleted.`)) {
          return;
        }
        try {
          await window.OpsCenter.apiFetch(`/api/deals/${card.id}`, { method: "DELETE" });
        } catch (err) {
          window.alert(err.message || "Failed to delete the item");
          return;
        }
        await loadBoard();
      });
      el.appendChild(delBtn);

      el.addEventListener("click", () => {
        window.location.href = `/deals/${card.id}`;
      });

      el.addEventListener("dragstart", (event) => {
        draggedDealId = card.id;
        el.classList.add("dragging");
        if (event.dataTransfer) {
          event.dataTransfer.effectAllowed = "move";
          event.dataTransfer.setData("text/plain", String(card.id));
        }
      });

      el.addEventListener("dragend", () => {
        el.classList.remove("dragging");
        draggedDealId = null;
      });

      return el;
    }

    // renderColumn(stage): builds a stage column with a header, counter and body.
    // If is_terminal=true, the column is collapsed (no cards, just a link to the archive).
    // Otherwise it renders all stage.cards. dragover/drop handlers on the column
    // accept dropped cards -> POST /api/deals/{id}/move and a full board re-fetch.
    function renderColumn(stage) {
      const col = document.createElement("div");
      col.className = "board-column";
      if (stage.is_terminal) col.classList.add("board-column--terminal");
      col.dataset.stageId = String(stage.stage_id);

      const header = document.createElement("div");
      header.className = "board-column-header";

      const title = document.createElement("span");
      title.className = "board-column-title";
      title.textContent = stage.name;
      header.appendChild(title);

      const count = document.createElement("span");
      count.className = "board-column-count";
      count.textContent = String(stage.count);
      header.appendChild(count);

      col.appendChild(header);

      const body = document.createElement("div");
      body.className = "board-column-body";

      if (stage.is_terminal) {
        // The terminal column ("Done") is collapsed -- no card list, just the
        // counter (already rendered above) and a link to the full archive.
        const link = document.createElement("a");
        link.className = "board-column-terminal-link";
        link.href = "/archive";
        link.textContent = "Archive →";
        body.appendChild(link);
      } else {
        const cards = stage.cards || [];
        for (const card of cards) {
          body.appendChild(renderCard(card));
        }
      }
      col.appendChild(body);

      // ---------- Drop zone: move a card into this column (dragover/drop) ----------
      // dragover: visually highlight the column when a card is dragged over it.
      col.addEventListener("dragover", (event) => {
        if (draggedDealId == null) return;
        event.preventDefault();
        if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
        col.classList.add("drag-over");
      });

      // dragleave: remove the highlight when the drag leaves the column.
      col.addEventListener("dragleave", (event) => {
        if (col.contains(event.relatedTarget)) return;
        col.classList.remove("drag-over");
      });

      // drop: finish moving the card (draggedDealId) -> POST /api/deals/{id}/move
      // and a full board re-fetch to refresh days-in-stage and aging levels.
      col.addEventListener("drop", async (event) => {
        event.preventDefault();
        col.classList.remove("drag-over");
        const dealId = draggedDealId;
        draggedDealId = null;
        if (dealId == null) return;
        try {
          await window.OpsCenter.apiFetch(`/api/deals/${dealId}/move`, {
            method: "POST",
            body: { stage_id: stage.stage_id },
          });
        } catch (err) {
          window.alert(err.message || "Failed to move the item");
          return;
        }
        // A full board re-fetch: simpler and more reliable than an optimistic
        // local move -- guaranteed fresh days_in_stage/aging_level/counters
        // (including closing/reopening an item on entering/leaving the terminal stage).
        await loadBoard();
      });

      return col;
    }

    function renderBoard(columns) {
      columnsEl.innerHTML = "";
      if (!columns || columns.length === 0) {
        if (emptyEl) emptyEl.hidden = false;
        return;
      }
      if (emptyEl) emptyEl.hidden = true;
      const frag = document.createDocumentFragment();
      for (const stage of columns) {
        frag.appendChild(renderColumn(stage));
      }
      columnsEl.appendChild(frag);
    }

    async function loadBoard() {
      let columns;
      try {
        columns = await window.OpsCenter.apiFetch("/api/board");
      } catch (_err) {
        columnsEl.innerHTML = "";
        const p = document.createElement("p");
        p.className = "page-hint";
        p.textContent = "Failed to load the board.";
        columnsEl.appendChild(p);
        return;
      }
      renderBoard(columns);
    }

    reloadBoardRef = loadBoard;

    // ---------- "+ Item" modal: a minimal item-creation dialog ----------
    // openModal() / closeModal() control the modal's visibility and state.
    // The submit handler validates title (required) and optional fields (company, partner),
    // then POST /api/deals, refreshes the board and closes the modal.
    function openModal() {
      if (!modal) return;
      modal.hidden = false;
      if (errorEl) errorEl.hidden = true;
      if (form) form.reset();
      const titleInput = form ? form.querySelector('[name="title"]') : null;
      if (titleInput) titleInput.focus();
    }

    function closeModal() {
      if (!modal) return;
      modal.hidden = true;
    }

    if (newDealBtn) newDealBtn.addEventListener("click", openModal);
    if (closeBtn) closeBtn.addEventListener("click", closeModal);
    if (cancelBtn) cancelBtn.addEventListener("click", closeModal);
    if (modal) {
      modal.addEventListener("click", (event) => {
        if (event.target === modal) closeModal();
      });
    }
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && modal && !modal.hidden) closeModal();
    });

    if (form) {
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const data = new FormData(form);
        const title = String(data.get("title") || "").trim();
        if (!title) {
          if (errorEl) {
            errorEl.textContent = "Title is required";
            errorEl.hidden = false;
          }
          return;
        }
        const payload = { title };
        for (const key of ["company", "partner"]) {
          const value = String(data.get(key) || "").trim();
          if (value) payload[key] = value;
        }
        try {
          await window.OpsCenter.apiFetch("/api/deals", { method: "POST", body: payload });
        } catch (err) {
          if (errorEl) {
            errorEl.textContent = err.message || "Failed to create the item";
            errorEl.hidden = false;
          }
          return;
        }
        closeModal();
        await loadBoard();
      });
    }

    loadBoard();
  }

  // Escalation-ladder badges for the "Ping Today" panel: the step is computed
  // (not stored in the DB), it arrives here as a ready number 1..3.
  function pingStepBadge(step) {
    if (step >= 3) {
      return { cls: "ping-step-3", text: "Escalate: to manager/partner" };
    }
    if (step === 2) {
      return { cls: "ping-step-2", text: "follow-up ping" };
    }
    return { cls: "ping-step-1", text: "ping" };
  }

  // Copying a ready ping string -- the same pattern as "Slice" (initSliceModal
  // below): the Clipboard API, and when unavailable -- selecting the text node
  // (textContent, not innerHTML) and document.execCommand('copy'). textEl is the
  // ping-string element in the DOM (its textContent is already safely inserted,
  // see renderPingRow).
  async function copyPingText(textEl, feedbackEl) {
    const text = textEl.textContent || "";

    function show(message) {
      if (!feedbackEl) return;
      feedbackEl.textContent = message;
      feedbackEl.hidden = false;
      window.setTimeout(() => {
        feedbackEl.hidden = true;
      }, 3000);
    }

    try {
      if (!navigator.clipboard || !navigator.clipboard.writeText) {
        throw new Error("Clipboard API unavailable");
      }
      await navigator.clipboard.writeText(text);
      show("Copied");
    } catch (_err) {
      try {
        const range = document.createRange();
        range.selectNodeContents(textEl);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        const copied = document.execCommand && document.execCommand("copy");
        show(copied ? "Copied" : "Text selected -- press Ctrl+C");
      } catch (_fallbackErr) {
        show("Could not copy automatically -- select the text manually");
      }
    }
  }

  // initPingPanel(): a collapsible "Ping Today" panel above .board-columns.
  // Data -- a separate GET /api/pings (the page's second fetch besides /api/board
  // from loadBoard()); row actions ("Pinged", "Snooze until...") do their own
  // re-fetch (only /api/pings for "Pinged"; /api/pings + /api/board for a snooze,
  // since a snoozed-item badge appears on the board card). CRITICAL:
  // title/stage_name/ping_text are user data, inserted EXCLUSIVELY via
  // textContent/createElement, never innerHTML with data from /api/pings.
  const PING_COLLAPSE_KEY = "opscenter.pingPanelCollapsed";

  function initPingPanel() {
    const panel = document.querySelector("[data-ping-panel]");
    if (!panel) return;

    const toggleBtn = panel.querySelector("[data-ping-panel-toggle]");
    const bodyEl = panel.querySelector("[data-ping-panel-body]");
    const chevronEl = panel.querySelector("[data-ping-chevron]");
    const countEl = panel.querySelector("[data-ping-count]");
    const emptyEl = panel.querySelector("[data-ping-empty]");
    const listEl = panel.querySelector("[data-ping-list]");
    const errorEl = panel.querySelector("[data-ping-error]");

    const showPingError = window.OpsCenter.makeErrorShower(errorEl, 5000);

    function setCollapsed(collapsed) {
      if (bodyEl) bodyEl.hidden = collapsed;
      if (toggleBtn) toggleBtn.setAttribute("aria-expanded", String(!collapsed));
      if (chevronEl) chevronEl.textContent = collapsed ? "▸" : "▾";
      try {
        window.localStorage.setItem(PING_COLLAPSE_KEY, collapsed ? "1" : "0");
      } catch (_err) {
        /* localStorage unavailable (private mode) -- the state just won't survive F5 */
      }
    }

    let collapsedInitial = false;
    try {
      collapsedInitial = window.localStorage.getItem(PING_COLLAPSE_KEY) === "1";
    } catch (_err) {
      collapsedInitial = false;
    }
    setCollapsed(collapsedInitial);

    if (toggleBtn) {
      toggleBtn.addEventListener("click", () => {
        setCollapsed(!(bodyEl && bodyEl.hidden));
      });
    }

    // renderPingRow(item): one row of the block. All item fields -- user data
    // (title/stage_name/ping_text) -- go into the DOM only via textContent, verbatim.
    function renderPingRow(item) {
      const row = document.createElement("div");
      row.className = "ping-row";
      if (item.escalate) row.classList.add("ping-row--escalate");
      row.dataset.dealId = String(item.deal_id);

      const header = document.createElement("div");
      header.className = "ping-row-header";

      const titleLink = document.createElement("a");
      titleLink.className = "ping-row-title";
      titleLink.href = `/deals/${item.deal_id}`;
      titleLink.textContent = item.title;
      header.appendChild(titleLink);

      const stageEl = document.createElement("span");
      stageEl.className = "ping-row-stage";
      stageEl.textContent = item.stage_name;
      header.appendChild(stageEl);

      const daysBadge = document.createElement("span");
      daysBadge.className = "badge aging-overdue";
      daysBadge.textContent = `${item.days_since_activity} business days without activity`;
      header.appendChild(daysBadge);

      const stepInfo = pingStepBadge(item.escalation_step);
      const stepBadge = document.createElement("span");
      stepBadge.className = `badge ${stepInfo.cls}`;
      stepBadge.textContent = stepInfo.text;
      header.appendChild(stepBadge);

      row.appendChild(header);

      const textEl = document.createElement("p");
      textEl.className = "ping-row-text";
      textEl.textContent = item.ping_text;
      row.appendChild(textEl);

      const actions = document.createElement("div");
      actions.className = "ping-row-actions";

      const copyBtn = document.createElement("button");
      copyBtn.type = "button";
      copyBtn.className = "small";
      copyBtn.textContent = "Copy ping";
      actions.appendChild(copyBtn);

      const copyFeedback = document.createElement("span");
      copyFeedback.className = "ping-copy-feedback";
      copyFeedback.hidden = true;
      actions.appendChild(copyFeedback);

      copyBtn.addEventListener("click", () => {
        copyPingText(textEl, copyFeedback);
      });

      const pingBtn = document.createElement("button");
      pingBtn.type = "button";
      pingBtn.className = "small";
      pingBtn.textContent = "Pinged";
      pingBtn.addEventListener("click", async () => {
        pingBtn.disabled = true;
        try {
          await window.OpsCenter.apiFetch(`/api/deals/${item.deal_id}/ping`, { method: "POST" });
        } catch (err) {
          showPingError(err.message || "Failed to record the ping");
          pingBtn.disabled = false;
          return;
        }
        // Only a /api/pings re-fetch -- "Pinged" does not touch last_activity_at
        // and does not change board cards, so a full board re-fetch is not needed.
        await loadPings();
      });
      actions.appendChild(pingBtn);

      const snoozeBtn = document.createElement("button");
      snoozeBtn.type = "button";
      snoozeBtn.className = "small";
      snoozeBtn.textContent = "Snooze until…";
      actions.appendChild(snoozeBtn);

      const dateInput = document.createElement("input");
      dateInput.type = "date";
      dateInput.className = "ping-date-input";
      dateInput.hidden = true;
      const tomorrow = new Date();
      tomorrow.setDate(tomorrow.getDate() + 1);
      const y = tomorrow.getFullYear();
      const m = String(tomorrow.getMonth() + 1).padStart(2, "0");
      const d = String(tomorrow.getDate()).padStart(2, "0");
      dateInput.min = `${y}-${m}-${d}`;
      actions.appendChild(dateInput);

      snoozeBtn.addEventListener("click", () => {
        dateInput.hidden = !dateInput.hidden;
        if (!dateInput.hidden) {
          dateInput.focus();
          if (typeof dateInput.showPicker === "function") {
            try {
              dateInput.showPicker();
            } catch (_err) {
              /* showPicker unavailable in this context -- the user opens the calendar themselves */
            }
          }
        }
      });

      dateInput.addEventListener("change", async () => {
        const value = dateInput.value;
        if (!value) return;
        try {
          await window.OpsCenter.apiFetch(`/api/deals/${item.deal_id}/snooze`, {
            method: "POST",
            body: { until: value },
          });
        } catch (err) {
          showPingError(err.message || "The date must be in the future");
          return;
        }
        dateInput.hidden = true;
        dateInput.value = "";
        // A snooze moves the "snoozed until..." badge on the board card -- needs
        // a board re-fetch in addition to the /api/pings re-fetch (see the comment above initPingPanel).
        await loadPings();
        if (reloadBoardRef) await reloadBoardRef();
      });

      row.appendChild(actions);
      return row;
    }

    function renderPingPanel(data) {
      const items = (data && data.items) || [];
      const count = data && typeof data.count === "number" ? data.count : items.length;
      if (countEl) countEl.textContent = String(count);

      listEl.innerHTML = "";
      if (!items.length) {
        if (emptyEl) emptyEl.hidden = false;
        return;
      }
      if (emptyEl) emptyEl.hidden = true;
      const frag = document.createDocumentFragment();
      for (const item of items) {
        frag.appendChild(renderPingRow(item));
      }
      listEl.appendChild(frag);
    }

    async function loadPings() {
      let data;
      try {
        data = await window.OpsCenter.apiFetch("/api/pings");
      } catch (_err) {
        data = { count: 0, items: [] };
        showPingError("Failed to load the “Ping Today” panel");
      }
      renderPingPanel(data);
    }

    loadPings();
  }

  // initSliceModal(): the "Slice" button on the board -> GET /api/board/slice ->
  // a modal with plain text in a <pre> (grouped by stages by the backend) and a
  // "Copy" button (Clipboard API with a fallback to selecting the <pre> text +
  // document.execCommand('copy') when the Clipboard API is unavailable -- e.g. a
  // non-secure context).
  // CRITICAL: the text from the API is already safe for <pre> -- it is plain
  // text inserted via textContent (not innerHTML), so any <script> or other
  // HTML tags are shown literally, without interpretation.
  function initSliceModal() {
    const btn = document.querySelector("[data-slice-btn]");
    const modal = document.querySelector("[data-slice-modal]");
    if (!btn || !modal) return;

    const textEl = modal.querySelector("[data-slice-text]");
    const emptyEl = modal.querySelector("[data-slice-empty]");
    const copyBtn = modal.querySelector("[data-slice-copy]");
    const feedbackEl = modal.querySelector("[data-slice-feedback]");
    const closeBtn = modal.querySelector("[data-modal-close]");
    const cancelBtn = modal.querySelector("[data-modal-cancel]");

    function showFeedback(text) {
      if (!feedbackEl) return;
      feedbackEl.textContent = text;
      feedbackEl.hidden = false;
    }

    function hideFeedback() {
      if (!feedbackEl) return;
      feedbackEl.hidden = true;
      feedbackEl.textContent = "";
    }

    function openModal() {
      modal.hidden = false;
      hideFeedback();
      loadSlice();
    }

    function closeModal() {
      modal.hidden = true;
    }

    async function loadSlice() {
      textEl.textContent = "Loading…";
      if (emptyEl) emptyEl.hidden = true;
      let text = "";
      try {
        const data = await window.OpsCenter.apiFetch("/api/board/slice");
        text = (data && data.text) || "";
      } catch (_err) {
        text = "";
      }
      textEl.textContent = text;
      if (emptyEl) emptyEl.hidden = Boolean(text);
    }

    if (copyBtn) {
      copyBtn.addEventListener("click", async () => {
        const text = textEl.textContent || "";
        try {
          if (!navigator.clipboard || !navigator.clipboard.writeText) {
            throw new Error("Clipboard API unavailable");
          }
          await navigator.clipboard.writeText(text);
          showFeedback("Copied");
        } catch (_err) {
          // Fallback: select the <pre> contents so the user can copy manually
          // (Ctrl+C) or via the browser context menu.
          try {
            const range = document.createRange();
            range.selectNodeContents(textEl);
            const selection = window.getSelection();
            selection.removeAllRanges();
            selection.addRange(range);
            const copied = document.execCommand && document.execCommand("copy");
            showFeedback(copied ? "Copied" : "Text selected -- press Ctrl+C");
          } catch (_fallbackErr) {
            showFeedback("Could not copy automatically -- select the text manually");
          }
        }
      });
    }

    btn.addEventListener("click", openModal);
    if (closeBtn) closeBtn.addEventListener("click", closeModal);
    if (cancelBtn) cancelBtn.addEventListener("click", closeModal);
    modal.addEventListener("click", (event) => {
      if (event.target === modal) closeModal();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !modal.hidden) closeModal();
    });
  }

  // initArchivePage(): renders the archive page of closed items (/archive).
  // GET /api/deals/archive returns closed items sorted by closed_at DESC.
  // renderItem(deal) builds a clickable link to the card with the title, company and close date.
  function initArchivePage() {
    const page = document.querySelector('[data-page="archive"]');
    if (!page) return;

    const listEl = page.querySelector("[data-archive-list]");
    const emptyEl = page.querySelector("[data-archive-empty]");

    // renderItem(deal): builds a DOM link to the item card with the fields
    // title, company (if present) and close date (closed_at) in DD.MM.YYYY format.
    function renderItem(deal) {
      const link = document.createElement("a");
      link.className = "archive-item";
      link.href = `/deals/${deal.id}`;

      const title = document.createElement("span");
      title.className = "archive-item-title";
      title.textContent = deal.title;
      link.appendChild(title);

      if (deal.company) {
        const company = document.createElement("span");
        company.className = "archive-item-company";
        company.textContent = deal.company;
        link.appendChild(company);
      }

      const dateEl = document.createElement("span");
      dateEl.className = "archive-item-date";
      dateEl.textContent = window.formatDate ? window.formatDate(deal.closed_at) : deal.closed_at || "";
      link.appendChild(dateEl);

      return link;
    }

    // loadArchive(): fetches GET /api/deals/archive and renders the list of closed items.
    // Shows an empty message if the list is empty.
    async function loadArchive() {
      let deals;
      try {
        deals = await window.OpsCenter.apiFetch("/api/deals/archive");
      } catch (_err) {
        deals = [];
      }
      listEl.innerHTML = "";
      if (!deals || deals.length === 0) {
        if (emptyEl) emptyEl.hidden = false;
        return;
      }
      if (emptyEl) emptyEl.hidden = true;
      const frag = document.createDocumentFragment();
      for (const deal of deals) {
        frag.appendChild(renderItem(deal));
      }
      listEl.appendChild(frag);
    }

    loadArchive();
  }

  document.addEventListener("DOMContentLoaded", () => {
    initBoardPage();
    initPingPanel();
    initSliceModal();
    initArchivePage();
  });
})();
