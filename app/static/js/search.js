// Header search: a debounced request to GET /api/search, renders a dropdown
// with "Items"/"Notes" groups in [data-search-dropdown], closes on Esc/click
// outside/navigating to a result. Loaded on every page via base.html (a plain
// <script>, not a module), since the header is shared across all pages.
// All network requests are same-origin /api/... only, via window.OpsCenter.apiFetch.
"use strict";

(function () {
  const DEBOUNCE_MS = 250;

  /**
   * Prepares an FTS5 snippet for insertion via innerHTML.
   *
   * CRITICAL: the backend returns a snippet with highlight markers [b]/[/b]
   * wrapping RAW user text (a note body may contain anything, including
   * `<script>`/`<img onerror=...>`). So the order is mandatory:
   *   1. First escape the ENTIRE snippet text (escapeHtml) -- after this step
   *      the string is guaranteed to contain no "live" tags.
   *   2. Only then replace the already-escaped literals "[b]"/"[/b]" with real
   *      <b>/</b> tags for highlighting.
   * The order cannot be swapped: replacing the markers with <b> first and then
   * escaping would turn the <b> tags themselves into &lt;b&gt; and the
   * highlighting would disappear; inserting the raw snippet via innerHTML
   * without escaping at all would be self-XSS via the note body.
   */
  function renderSnippetHtml(snippet) {
    const escaped = window.OpsCenter.escapeHtml(snippet || "");
    return escaped.split("[b]").join("<b>").split("[/b]").join("</b>");
  }

  function noteStatusLabel(status) {
    if (status === "archived") return "Archived";
    if (status === "deferred") return "Deferred";
    if (status === "attached") return "Attached";
    return "Inbox";
  }

  function initHeaderSearch() {
    const input = document.querySelector("[data-search-input]");
    const dropdown = document.querySelector("[data-search-dropdown]");
    if (!input || !dropdown) return;

    // debounceTimer + requestSeq: protection against a race of stale server responses.
    // debounceTimer debounces input (groups quick successive requests into one),
    // requestSeq invalidates in-flight requests if the user changed the input
    // before the response arrives (the old request is ignored in runSearch).
    let debounceTimer = null;
    let requestSeq = 0;

    function closeDropdown() {
      dropdown.hidden = true;
      dropdown.innerHTML = "";
    }

    function openDropdown() {
      dropdown.hidden = false;
    }

    function renderEmptyState() {
      dropdown.innerHTML = "";
      const empty = document.createElement("div");
      empty.className = "search-empty";
      empty.textContent = "Nothing found";
      dropdown.appendChild(empty);
      openDropdown();
    }

    function appendGroupTitle(frag, text) {
      const title = document.createElement("div");
      title.className = "search-group-title";
      title.textContent = text;
      frag.appendChild(title);
    }

    // Navigation on clicking a note result. Notes without deal_id (never
    // attached to an item) have no card of their own to navigate to: in that
    // case we send the user to the Inbox (navigate to root "/"), without a
    // filter query parameter (the note status is visible right in the search
    // result, switching the feed filter is one click).
    function goToNote(note) {
      closeDropdown();
      input.blur();
      if (note.deal_id != null) {
        window.location.href = `/deals/${note.deal_id}`;
      } else {
        window.location.href = "/";
      }
    }

    function buildDealResult(deal) {
      const item = document.createElement("a");
      item.href = `/deals/${deal.id}`;
      item.className = "search-result";

      const title = document.createElement("div");
      title.className = "search-result-title";
      title.textContent = deal.title || "";
      item.appendChild(title);

      const snippet = document.createElement("div");
      snippet.className = "search-result-snippet";
      // Safe: renderSnippetHtml escapes HTML before inserting [b]/[/b].
      snippet.innerHTML = renderSnippetHtml(deal.snippet);
      item.appendChild(snippet);

      item.addEventListener("click", (event) => {
        event.preventDefault();
        closeDropdown();
        input.blur();
        window.location.href = `/deals/${deal.id}`;
      });

      return item;
    }

    function buildNoteResult(note) {
      const item = document.createElement("a");
      item.href = note.deal_id != null ? `/deals/${note.deal_id}` : "/";
      item.className = "search-result";
      // Note bodies are intentionally NOT linkified here. The whole result row
      // is an outer <a> (navigation to the item, event.preventDefault on click),
      // so a nested <a> from linkification would be invalid/dead markup, not a
      // lost feature.

      const snippet = document.createElement("div");
      snippet.className = "search-result-snippet";
      // Safe: renderSnippetHtml escapes HTML before inserting [b]/[/b].
      snippet.innerHTML = renderSnippetHtml(note.snippet);
      item.appendChild(snippet);

      const meta = document.createElement("div");
      meta.className = "search-result-meta";
      meta.textContent = noteStatusLabel(note.status);
      item.appendChild(meta);

      item.addEventListener("click", (event) => {
        event.preventDefault();
        goToNote(note);
      });

      return item;
    }

    function renderResults(data) {
      const deals = Array.isArray(data && data.deals) ? data.deals : [];
      const notes = Array.isArray(data && data.notes) ? data.notes : [];

      if (!deals.length && !notes.length) {
        renderEmptyState();
        return;
      }

      const frag = document.createDocumentFragment();

      if (deals.length) {
        appendGroupTitle(frag, "Items");
        for (const deal of deals) frag.appendChild(buildDealResult(deal));
      }

      if (notes.length) {
        appendGroupTitle(frag, "Notes");
        for (const note of notes) frag.appendChild(buildNoteResult(note));
      }

      dropdown.innerHTML = "";
      dropdown.appendChild(frag);
      openDropdown();
    }

    async function runSearch(query) {
      const seq = ++requestSeq;
      let data;
      try {
        data = await window.OpsCenter.apiFetch(`/api/search?q=${encodeURIComponent(query)}`);
      } catch (_err) {
        // Garbage input must still return 200 with empty groups (backend);
        // a network/other error -- just show an empty dropdown, without breaking the UI.
        if (seq === requestSeq) renderEmptyState();
        return;
      }
      if (seq !== requestSeq) return; // response to a stale request -- ignore it
      renderResults(data);
    }

    input.addEventListener("input", () => {
      const query = input.value.trim();
      if (debounceTimer) clearTimeout(debounceTimer);
      if (!query) {
        requestSeq += 1; // invalidate in-flight requests
        closeDropdown();
        return;
      }
      debounceTimer = setTimeout(() => runSearch(query), DEBOUNCE_MS);
    });

    document.addEventListener("click", (event) => {
      if (dropdown.hidden) return;
      if (event.target === input || dropdown.contains(event.target)) return;
      closeDropdown();
    });

    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if (dropdown.hidden) return;
      closeDropdown();
      if (document.activeElement === input) input.blur();
    });
  }

  document.addEventListener("DOMContentLoaded", initHeaderSearch);
})();
