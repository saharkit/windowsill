#!/usr/bin/env node
// Emits the handbook's standing rules on SessionStart. Its stdout becomes context the
// model sees for the session.
//
// Written exec-form on purpose. The obvious shell form — {"command": "echo 'RULE: ...'"} —
// is POSIX-only: cmd.exe does not strip single quotes, so on Windows the rule would arrive
// wrapped in literal quotes. That is worse than a crash, because it looks like it worked.
//
// Two ordered sources, both delivered under one provenance line. The order is part of the
// contract: the flagellant rule ships first, the non-stop rule second. They parse
// independently, so they are not guaranteed identical on malformed input — same parser,
// not one parser.

const fs = require("fs");
const path = require("path");

// Read one source into a stripped body. Returns the body string on success; throws with a
// source-bearing message on every failure mode (BOM-strip failures don't happen — the regex
// never throws — but every other failure path is named). The caller catches and routes to
// the per-source stderr line so a failed source never suppresses the next source.
function readBody(source) {
  // Strip a UTF-8 BOM first. Without this the front-matter match below is anchored past it,
  // silently emits nothing, and the whole file — YAML keys included — lands in the context
  // of every session. A BOM is what several Windows editors add on save, which is exactly
  // the platform this file is careful about elsewhere.
  const raw = fs.readFileSync(source, "utf8").replace(/^\uFEFF/, "");

  // The front matter addresses the output-style loader, not the reader. The pattern tolerates
  // leading blank lines and trailing spaces on either fence: this strip fails OPEN — when it
  // does not match, the YAML ships to every session — so it must not be brittle about
  // whitespace an ordinary edit can introduce.
  const body = raw.replace(/^\s*---[ \t]*\r?\n[\s\S]*?\r?\n---[ \t]*\r?\n/, "").trim();

  if (!body) throw new Error("no body after front matter in " + source);
  // The sanity check is anchored to the first line of the stripped body: if YAML keys survived
  // (because the strip did not match), the very first line names what file the leak came from
  // in the next reader's head. Front-matter that DOES NOT contain those keys is not flagged —
  // a hook without `name:` is still a hook, the contract is about accidental leakage, not
  // about the loader keys being exhaustive.
  if (/^\s*(name|description|keep-coding-instructions)\s*:/m.test(body.split("\n")[0])) {
    throw new Error("front matter survived the strip in " + source);
  }
  return body;
}

// Emit the standing rules from a list of source paths. Returns:
//   { ok: true,  stdout: <combined text>, perSourceStderr: <array of lines> }
//   { ok: false, err: <Error>, perSourceStderr: <array of lines> }
// On ok, `stdout` already carries the provenance line and the single-blank-line separator
// between bodies. `perSourceStderr` is the list of per-source failure lines, in source order;
// empty when every source loaded cleanly.
function emitRules(sources) {
  const bodies = [];
  const perSourceStderr = [];
  for (const source of sources) {
    try {
      bodies.push(readBody(source));
    } catch (err) {
      // Per-source: a failed load never suppresses the next source. The stderr line names
      // which source failed so a broken path is distinguishable from a deliberate absence.
      perSourceStderr.push(
        "agent-handbook: standing rule not emitted from " + source + ": " +
        (err && err.message) + "\n"
      );
    }
  }
  if (bodies.length === 0) {
    return { ok: false, err: new Error("no standing rules could be loaded"), perSourceStderr };
  }
  // Name the source. Without it these rules are indistinguishable from the user's own
  // instructions, and somebody wondering why their agent stopped re-checking has nothing to grep.
  // The provenance line is printed once, with a single blank line between bodies.
  const stdout = "From the agent-handbook plugin (standing rules):\n\n" + bodies.join("\n\n") + "\n";
  return { ok: true, stdout, perSourceStderr };
}

if (require.main === module) {
  // Explicit ordered list, never a directory scan: the order is the contract.
  const SOURCES = [
    path.join(__dirname, "..", "output-styles", "flagellant-rule.md"),
    path.join(__dirname, "..", "nonstop-rule.md"),
  ];
  try {
    const result = emitRules(SOURCES);
    if (result.perSourceStderr.length > 0) {
      process.stderr.write(result.perSourceStderr.join(""));
    }
    if (!result.ok) throw result.err;
    process.stdout.write(result.stdout);
  } catch (err) {
    // Never fail a session over a standing rule: a silent absence is recoverable, a blocked
    // start is not. But say WHY on stderr, which --debug captures and stdout never sees, so a
    // broken path is distinguishable from a deliberate absence instead of looking identical.
    process.stderr.write("agent-handbook: standing rules not emitted: " + (err && err.message) + "\n");
  }
}

module.exports = { emitRules, readBody };