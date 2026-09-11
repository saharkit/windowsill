// Pin the multi-source contract of the SessionStart hook. Behavior changes to merge-driving
// infrastructure (this fires in every session of everyone who installs the plugin) need
// tests at the seam where the script is invoked, so a renamed source, a re-ordered list or a
// regression to the literal-BOM regex shows red on the same PR rather than in the first
// session of a downstream user.
//
// Run with the Node built-in test runner — no install step:
//   node --test plugins/agent-handbook/tests/test_emit_standing_rules.js
//
// The test imports the hook module directly rather than spawning a child process: the hook
// is exec-form by design (the obvious shell form is POSIX-only — see emit-standing-rules.js
// header), and what we are pinning here is the BODY of the contract, not the wrapping
// interpreter. Spawning the script would only add a layer that masks the failure modes
// these tests are trying to surface.

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { emitRules, readBody } = require("../hooks/emit-standing-rules.js");

// Make a temp directory whose contents each test fills with the sources it wants. Cleaned
// up in `finally` so a failing assertion still releases the directory — without that, a
// developer re-running the suite accumulates /tmp tmpdirs that nothing tracks.
function makeTempDir(prefix) {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix + "-"));
}

function cleanup(dir) {
  fs.rmSync(dir, { recursive: true, force: true });
}

function writeSource(dir, name, content) {
  const file = path.join(dir, name);
  fs.writeFileSync(file, content);
  return file;
}

// Minimal valid source body: front matter with the three loader keys, a blank line, then a
// one-line body. Mirrors the shape of the real flagellant-rule.md / nonstop-rule.md at the
// barest level these tests care about.
function validSource(body) {
  return (
    "---\n" +
    "name: A standing rule\n" +
    "description: A description that names the rule.\n" +
    "keep-coding-instructions: true\n" +
    "---\n" +
    "\n" +
    body +
    "\n"
  );
}

test("source order is the contract — flagellant first, non-stop second", () => {
  const dir = makeTempDir("ah-order");
  try {
    const flagellant = writeSource(dir, "flagellant.md", validSource("FLAGELLANT BODY"));
    const nonstop = writeSource(dir, "nonstop.md", validSource("NONSTOP BODY"));

    const result = emitRules([flagellant, nonstop]);

    assert.equal(result.ok, true);
    const flagPos = result.stdout.indexOf("FLAGELLANT BODY");
    const nonstopPos = result.stdout.indexOf("NONSTOP BODY");
    assert.notEqual(flagPos, -1, "the flagellant body was not emitted");
    assert.notEqual(nonstopPos, -1, "the non-stop body was not emitted");
    assert.ok(
      flagPos < nonstopPos,
      "flagellant must come before non-stop (got flagellant at " +
        flagPos + ", nonstop at " + nonstopPos + ")"
    );
  } finally {
    cleanup(dir);
  }
});

test("per-source isolation — one failing source does not suppress the next", () => {
  const dir = makeTempDir("ah-isolation");
  try {
    const broken = writeSource(
      dir,
      "broken.md",
      "---\nname: broken\ndescription: x\nkeep-coding-instructions: true\n---\n\n"
    );
    const flagellant = writeSource(dir, "flagellant.md", validSource("FLAGELLANT BODY"));

    const result = emitRules([broken, flagellant]);

    assert.equal(result.ok, true);
    assert.ok(
      result.stdout.includes("FLAGELLANT BODY"),
      "a broken first source must not suppress the second source"
    );
    assert.equal(result.perSourceStderr.length, 1, "exactly one stderr line, for the broken source");
    assert.ok(
      result.perSourceStderr[0].includes(broken),
      "the per-source stderr line names the broken source path"
    );
    assert.ok(
      result.perSourceStderr[0].includes("no body after front matter"),
      "the per-source stderr line names the underlying cause"
    );
  } finally {
    cleanup(dir);
  }
});

test("provenance format — single line, exactly once", () => {
  const dir = makeTempDir("ah-provenance");
  try {
    const flagellant = writeSource(dir, "flagellant.md", validSource("FLAGELLANT BODY"));
    const nonstop = writeSource(dir, "nonstop.md", validSource("NONSTOP BODY"));

    const result = emitRules([flagellant, nonstop]);

    assert.equal(result.ok, true);
    assert.ok(
      result.stdout.startsWith("From the agent-handbook plugin (standing rules):\n\n"),
      "the stdout must start with the provenance line followed by a blank line"
    );
    // Exactly ONE provenance line — a reader grepping for "From the agent-handbook plugin"
    // must hit one anchor regardless of how many rules are emitted.
    const matches = result.stdout.match(/From the agent-handbook plugin/g) || [];
    assert.equal(matches.length, 1, "the provenance line appears exactly once (got " + matches.length + ")");
  } finally {
    cleanup(dir);
  }
});

test("separator — exactly one blank line between bodies", () => {
  const dir = makeTempDir("ah-separator");
  try {
    const flagellant = writeSource(dir, "flagellant.md", validSource("FIRST BODY"));
    const nonstop = writeSource(dir, "nonstop.md", validSource("SECOND BODY"));

    const result = emitRules([flagellant, nonstop]);

    assert.equal(result.ok, true);
    // The bodies are joined by "\n\n" — the section between two distinct single-line bodies
    // is therefore exactly "\n\n" (no triple-newline). A multi-line body that ends with a
    // trailing newline would add one more blank line — pin the single-line case here because
    // it is the cleanest expression of the contract.
    assert.ok(
      result.stdout.includes("FIRST BODY\n\nSECOND BODY"),
      "the bodies are separated by exactly one blank line"
    );
    assert.ok(
      !result.stdout.includes("FIRST BODY\n\n\nSECOND BODY"),
      "there must not be more than one blank line between bodies"
    );
  } finally {
    cleanup(dir);
  }
});

test("BOM strip — a leading UTF-8 BOM is removed before the front-matter match", () => {
  const dir = makeTempDir("ah-bom");
  try {
    // The leading BOM (﻿) is the byte sequence editors like Notepad prepend on save. With the
    // strip in place, the front matter parses and the body is emitted; without it, the YAML
    // lands in context of every session. Pin the GOOD path here.
    const withBom = writeSource(
      dir,
      "with-bom.md",
      "﻿" + validSource("BODY AFTER BOM")
    );
    // The first three bytes must literally be the BOM, otherwise the test is meaningless.
    const head = fs.readFileSync(withBom).slice(0, 3);
    assert.deepEqual(
      Array.from(head),
      [0xef, 0xbb, 0xbf],
      "the fixture must start with the UTF-8 BOM bytes"
    );

    const result = emitRules([withBom]);

    assert.equal(result.ok, true);
    assert.ok(
      result.stdout.includes("BODY AFTER BOM"),
      "the BOM must be stripped so the body reaches the reader"
    );
    // And the BOM itself must NOT reach the reader — that is the whole point of the strip.
    assert.ok(
      !result.stdout.includes("﻿"),
      "no BOM should appear anywhere in the emitted text"
    );
    assert.ok(
      !result.stdout.includes("name:"),
      "front matter must not leak when preceded by a BOM"
    );
  } finally {
    cleanup(dir);
  }
});

test("front-matter strip — YAML keys do not leak into the emitted body", () => {
  const dir = makeTempDir("ah-fm");
  try {
    const source = writeSource(dir, "flagellant.md", validSource("THE BODY"));

    const body = readBody(source);

    assert.ok(body.includes("THE BODY"), "the body survived the strip");
    for (const key of ["name:", "description:", "keep-coding-instructions:"]) {
      assert.ok(!body.includes(key), "front-matter key '" + key + "' must not leak past the strip");
    }
  } finally {
    cleanup(dir);
  }
});

test("sanity check — an empty body after the front-matter strip throws", () => {
  const dir = makeTempDir("ah-empty");
  try {
    // Front matter with the loader keys, but the body that follows is empty. The strip
    // succeeds, the trim leaves "", and the sanity guard fires so the whole file — YAML
    // keys and all — does NOT land in context as if it were a body.
    const empty = writeSource(
      dir,
      "empty.md",
      "---\nname: x\ndescription: x\nkeep-coding-instructions: true\n---\n\n   \n"
    );

    assert.throws(
      () => readBody(empty),
      /no body after front matter/,
      "an empty body must throw so it never reaches the reader"
    );
  } finally {
    cleanup(dir);
  }
});

test("sanity check — front matter that survives the strip is caught on the first line", () => {
  const dir = makeTempDir("ah-fm-survived");
  try {
    // No front matter at all. The strip is a no-op; the body the reader sees starts with
    // `name: …`, exactly the loader key the sanity check is anchored to.
    const noFm = writeSource(dir, "no-fm.md", "name: a rule\ndescription: not really\n\nrest\n");

    assert.throws(
      () => readBody(noFm),
      /front matter survived the strip/,
      "a file whose first line is a loader key must throw so YAML does not leak into context"
    );
  } finally {
    cleanup(dir);
  }
});

test("all sources fail — emit returns ok=false and names the underlying cause", () => {
  const dir = makeTempDir("ah-allfail");
  try {
    const a = writeSource(dir, "a.md", "name: a\n");      // no front matter → sanity fail
    const b = path.join(dir, "does-not-exist.md");          // readFileSync throws

    const result = emitRules([a, b]);

    assert.equal(result.ok, false, "an all-fail result must report ok=false");
    assert.ok(result.err instanceof Error, "the result carries the underlying Error");
    assert.equal(result.err.message, "no standing rules could be loaded");
    assert.equal(result.perSourceStderr.length, 2, "both sources' stderr lines are preserved");
    assert.ok(result.perSourceStderr[0].includes(a), "first stderr line names the first source");
    assert.ok(result.perSourceStderr[1].includes(b), "second stderr line names the second source");
  } finally {
    cleanup(dir);
  }
});