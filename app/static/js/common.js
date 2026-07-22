// Shared OpsCenter helpers, loaded on every page via
// <script src="/static/js/common.js"> before the page-specific script.
// All network requests are same-origin /api/... only.
"use strict";

(function () {
  function pad2(n) {
    return String(n).padStart(2, "0");
  }

  // Note type labels. Shared source for the type badge and the triage
  // suggestion banner (inbox.js, deal.js). "info" is the "for information" type.
  const NOTE_TYPE_LABELS = {
    status: "Status",
    task: "Task",
    agreement: "Agreement",
    reminder: "Reminder",
    info: "Info",
  };

  /**
   * Escapes HTML special characters in user text for safe insertion via
   * innerHTML. The ampersand is escaped first (into &amp;), otherwise later
   * entities get double-escaped (e.g. & in &lt; -> &amp;lt;).
   */
  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  /**
   * Escapes text, THEN wraps http(s) links in <a target="_blank"
   * rel="noopener noreferrer">. Returns an HTML string for innerHTML.
   *
   * CRITICAL (self-XSS): order is mandatory. escapeHtml runs first -- after
   * that step the string is guaranteed to contain no "live" tags (a note body
   * may contain `<script>` etc.). Only then do we insert <a>. Reversing the
   * order = self-XSS: linkifying raw text would let tags through into the DOM,
   * and escaping AFTER linkification would corrupt the just-inserted <a>.
   * A URL contains no spaces and no `<` (the regex excludes `<`), so inserted
   * tags/entities cannot end up inside href.
   */
  function linkifyEscaped(text) {
    return escapeHtml(text).replace(
      /https?:\/\/[^\s<]+/g,
      (url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`,
    );
  }

  /**
   * Formats an ISO date/time (stored in UTC, without a Z suffix) as
   * DD.MM.YYYY in the browser's local time zone.
   * @param {string} isoString
   * @returns {string}
   */
  function formatDate(isoString) {
    if (!isoString) return "";
    const hasTz = /Z$|[+-]\d{2}:?\d{2}$/.test(isoString);
    const d = new Date(hasTz ? isoString : `${isoString}Z`);
    if (Number.isNaN(d.getTime())) return "";
    return `${pad2(d.getDate())}.${pad2(d.getMonth() + 1)}.${d.getFullYear()}`;
  }

  /**
   * fetch helper for same-origin /api/...: serializes an object body to JSON,
   * throws an Error with the detail text on non-2xx, parses the JSON response
   * by default.
   * @param {string} url
   * @param {RequestInit} [options]
   */
  async function apiFetch(url, options) {
    const opts = Object.assign({}, options || {});
    if (
      opts.body &&
      typeof opts.body === "object" &&
      !(opts.body instanceof FormData) &&
      !(opts.body instanceof Blob)
    ) {
      opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
      opts.body = JSON.stringify(opts.body);
    }

    const resp = await fetch(url, opts);

    if (!resp.ok) {
      let detail = resp.statusText || `HTTP ${resp.status}`;
      try {
        const data = await resp.clone().json();
        if (data && data.detail) detail = data.detail;
      } catch (_err) {
        /* response body is not JSON -- keep statusText */
      }
      const error = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      error.status = resp.status;
      throw error;
    }

    if (resp.status === 204) return null;

    const contentType = resp.headers.get("content-type") || "";
    if (contentType.includes("application/json")) return resp.json();
    return resp.text();
  }

  /**
   * Refreshes the inbox counter in the current page header using data from
   * GET /api/inbox/summary. Safe to call on a page without a header.
   */
  async function refreshInboxCounter() {
    const valueEl = document.querySelector("[data-inbox-counter-value]");
    if (!valueEl) return;
    try {
      const summary = await apiFetch("/api/inbox/summary");
      valueEl.textContent = String(summary.inbox_count);
      const wrap = document.querySelector("[data-inbox-counter]");
      if (wrap) {
        wrap.classList.toggle("inbox-counter--alert", Boolean(summary.recovery_needed));
        wrap.title = summary.recovery_needed
          ? "The Inbox has piled up -- time to triage the Inbox"
          : "Inbox notes";
      }
    } catch (_err) {
      valueEl.textContent = "--";
    }
  }

  function isEditableTarget(el) {
    if (!el) return false;
    if (el.isContentEditable) return true;
    const tag = el.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
  }

  /**
   * Global "/" hotkey: focuses the quick-drop field on the Inbox/item card
   * (`[data-quick-drop]`), or, if that is absent on the page, the header
   * search field (`[data-search-input]`). Does not intercept "/" when focus is
   * already in a text field/textarea/select/contenteditable -- there the
   * character should be typed as usual.
   *
   * The quick-drop field (`[data-quick-drop]`) normally already holds the
   * user's unfinished input (serial note dropping) -- `select()` would select
   * all the text and the next keystroke would erase it. So for
   * `[data-quick-drop]` the cursor is placed at the end of the value
   * (`setSelectionRange`) without selecting. The header search field
   * (`[data-search-input]`) is usually empty or holds a previous query that is
   * fine to overwrite in one keystroke -- for it the `select()` (select all)
   * behavior is kept.
   */
  function initSlashHotkey() {
    document.addEventListener("keydown", (event) => {
      if (event.key !== "/") return;
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      if (isEditableTarget(document.activeElement)) return;

      const quickDrop = document.querySelector("[data-quick-drop]");
      const target = quickDrop || document.querySelector("[data-search-input]");
      if (!target) return;

      event.preventDefault();
      target.focus();
      if (quickDrop && typeof target.setSelectionRange === "function") {
        const end = target.value.length;
        target.setSelectionRange(end, end);
      } else if (typeof target.select === "function") {
        target.select();
      }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    initSlashHotkey();
    refreshInboxCounter();
  });

  function humanFileSize(bytes) {
    if (!Number.isFinite(bytes)) return "";
    if (bytes < 1024) return `${bytes} B`;
    const kb = bytes / 1024;
    if (kb < 1024) return `${kb.toFixed(1)} KB`;
    return `${(kb / 1024).toFixed(1)} MB`;
  }

  // renderAttachment: turns an attachment object into a DOM element (a link
  // with a preview for images, or a file link with size for everything else).
  function renderAttachment(att) {
    const isImage = (att.mime_type || "").startsWith("image/");
    const url = `/api/attachments/${att.id}`;
    if (isImage) {
      const link = document.createElement("a");
      link.className = "note-attachment-image-link";
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener";
      const img = document.createElement("img");
      img.className = "note-attachment-image";
      img.src = url;
      img.alt = att.original_name || "image";
      img.loading = "lazy";
      link.appendChild(img);
      return link;
    }
    const link = document.createElement("a");
    link.className = "note-attachment-file";
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener";
    const name = document.createElement("span");
    name.className = "note-attachment-name";
    name.textContent = att.original_name || "file";
    const size = document.createElement("span");
    size.className = "note-attachment-size";
    size.textContent = humanFileSize(att.size_bytes);
    link.appendChild(name);
    link.appendChild(size);
    return link;
  }

  /**
   * Error toaster factory: returns a showError(message) function that writes
   * the text into the given element, shows it, and automatically hides it
   * after ms milliseconds. Each call resets the previous timer.
   */
  function makeErrorShower(el, ms = 4000) {
    let timer = null;
    return function showError(message) {
      if (!el) return;
      el.textContent = message;
      el.hidden = false;
      if (timer) window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        el.hidden = true;
      }, ms);
    };
  }

  const OpsCenter = {
    formatDate,
    apiFetch,
    refreshInboxCounter,
    escapeHtml,
    linkifyEscaped,
    NOTE_TYPE_LABELS,
    humanFileSize,
    renderAttachment,
    makeErrorShower,
  };

  window.OpsCenter = OpsCenter;
  // Short alias for use in templates/other modules without an explicit namespace.
  window.formatDate = formatDate;
})();
