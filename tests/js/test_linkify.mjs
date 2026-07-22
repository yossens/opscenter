// Test for the pure linkifyEscaped helper from app/static/js/common.js.
// The repo has no JS framework, so we use only Node built-ins (assert + vm).
// common.js is a plain <script> (window-global, not an ES module), so we run it
// in a vm sandbox with window/document stubs and pull out
// window.OpsCenter.linkifyEscaped -- the REAL shipped code is tested.
//
// Run: node tests/js/test_linkify.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";

const here = path.dirname(fileURLToPath(import.meta.url));
const src = readFileSync(path.join(here, "..", "..", "app", "static", "js", "common.js"), "utf8");

const sandbox = {
  window: {},
  document: { addEventListener() {}, querySelector() { return null; } },
};
vm.createContext(sandbox);
vm.runInContext(src, sandbox);

const { linkifyEscaped } = sandbox.window.OpsCenter;
assert.equal(typeof linkifyEscaped, "function", "linkifyEscaped must be exported on window.OpsCenter");

// (1) <script> renders as inert escaped text; no live tag.
const outScript = linkifyEscaped("<script>alert(1)</script>");
assert.ok(!outScript.includes("<script>"), "raw <script> tag must not survive escaping");
assert.ok(outScript.includes("&lt;script&gt;"), "script must be HTML-entity escaped");

// (2) A URL becomes an anchor with exactly that href and safe attributes.
const outUrl = linkifyEscaped("see https://example.com/x next");
assert.match(
  outUrl,
  /<a href="https:\/\/example\.com\/x" target="_blank" rel="noopener noreferrer">https:\/\/example\.com\/x<\/a>/,
  "URL must be wrapped in a safe anchor with the exact href",
);

// (3) URL + <script> in one body: the script is escaped, the link is live.
const outBoth = linkifyEscaped("<script>alert(1)</script> and a link https://example.com/x");
assert.ok(!outBoth.includes("<script>"), "script must stay escaped when a URL is also present");
assert.ok(outBoth.includes("&lt;script&gt;"), "script escaped alongside a live link");
assert.ok(
  outBoth.includes('<a href="https://example.com/x" target="_blank" rel="noopener noreferrer">'),
  "live link present alongside escaped script",
);

// (4) Attribute breakout: a URL with an embedded quote must not break href="...".
// escapeHtml turns `"` into &quot; and `<`/`>` into &lt;/&gt; BEFORE linkifying,
// so the dangerous character ends up inside href as an entity, not as an
// attribute/tag delimiter.
const outAttr = linkifyEscaped('https://evil.com/"onmouseover="alert(1)');
assert.ok(!outAttr.includes('"onmouseover'), "raw quote must not break out of href attribute");
assert.ok(outAttr.includes("&quot;onmouseover"), "embedded quote must be entity-escaped inside href");

const outTagBreak = linkifyEscaped('https://evil.com/"><script>alert(1)</script>');
assert.ok(!outTagBreak.includes("<script>"), "injected <script> tag must not survive after a URL");
assert.ok(outTagBreak.includes("&lt;script&gt;"), "injected tag must be entity-escaped");

const outImg = linkifyEscaped('https://evil.com/"><img onerror=alert(1)>');
assert.ok(!outImg.includes("<img"), "injected <img> tag must not survive after a URL");
assert.ok(!outImg.includes('/">'), "quote+bracket must not close the href attribute early");

// (5) Edge case: null/undefined do not throw.
assert.equal(linkifyEscaped(null), "");
assert.equal(linkifyEscaped(undefined), "");

console.log("linkifyEscaped: all assertions passed");
