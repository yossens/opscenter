// Test for the pure pending-attachment buffer helpers addPendingFile/
// removePendingFileAt from app/static/js/inbox.js. The repo has no JS framework,
// so we use only Node built-ins (assert + vm). inbox.js is a plain <script>
// (IIFE, window-global), so we run it in a vm sandbox with window/document stubs.
// The helpers are exposed on window.OpsCenter BEFORE the `if (!page) return`
// guard, so in the sandbox (where document.querySelector -> null) they are still
// available -- the REAL shipped code is tested.
//
// Run: node tests/js/test_pending_files.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";

const here = path.dirname(fileURLToPath(import.meta.url));
const src = readFileSync(path.join(here, "..", "..", "app", "static", "js", "inbox.js"), "utf8");

const sandbox = {
  window: { OpsCenter: {} },
  document: { addEventListener() {}, querySelector() { return null; } },
};
vm.createContext(sandbox);
vm.runInContext(src, sandbox);

const { addPendingFile, removePendingFileAt } = sandbox.window.OpsCenter;
assert.equal(typeof addPendingFile, "function", "addPendingFile must be exported on window.OpsCenter");
assert.equal(typeof removePendingFileAt, "function", "removePendingFileAt must be exported on window.OpsCenter");

const a = { name: "a.png" };
const b = { name: "b.png" };
const c = { name: "c.png" };

// (1) add: returns a NEW array with the file appended at the end; input is not mutated.
const empty = [];
const one = addPendingFile(empty, a);
assert.deepEqual(one, [a], "file appended to buffer");
assert.equal(empty.length, 0, "add must not mutate the input array");

const two = addPendingFile(one, b);
assert.deepEqual(two, [a, b], "second file appended, order preserved");
assert.equal(one.length, 1, "add must not mutate the previous array");

// (2) remove: deletes exactly one file by index, returns a NEW array.
const three = addPendingFile(two, c); // [a, b, c]
const withoutMiddle = removePendingFileAt(three, 1);
assert.deepEqual(withoutMiddle, [a, c], "removes the file at the given index");
assert.deepEqual(three, [a, b, c], "remove must not mutate the input array");

const first = removePendingFileAt(three, 0);
assert.deepEqual(first, [b, c], "removes the first file");

const last = removePendingFileAt(three, 2);
assert.deepEqual(last, [a, b], "removes the last file");

// (3) remove down to empty.
const drained = removePendingFileAt([a], 0);
assert.deepEqual(drained, [], "removing the only file yields an empty buffer");

console.log("pending-files buffer: all assertions passed");
