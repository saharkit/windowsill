#!/usr/bin/env node
// Emits the handbook's standing rules on SessionStart. Its stdout becomes context the
// model sees for the session.
//
// Written exec-form on purpose. The obvious shell form — {"command": "echo 'RULE: ...'"} —
// is POSIX-only: cmd.exe does not strip single quotes, so on Windows the rule would arrive
// wrapped in literal quotes. That is worse than a crash, because it looks like it worked.
//
// One source: the text lives in the output style beside this file, so both delivery
// surfaces read the same bytes. They parse it independently, so they are not guaranteed
// identical on malformed input — same source, not one parser.

const fs = require("fs");
const path = require("path");

const SOURCE = path.join(__dirname, "..", "output-styles", "flagellant-rule.md");

try {
  // Strip a UTF-8 BOM first. Without this the front-matter match below is anchored past it,
  // silently emits nothing, and the whole file — YAML keys included — lands in the context
  // of every session. A BOM is what several Windows editors add on save, which is exactly
  // the platform this file is careful about elsewhere.
  const raw = fs.readFileSync(SOURCE, "utf8").replace(/^\uFEFF/, "");

  // The front matter addresses the output-style loader, not the reader. The pattern tolerates
  // leading blank lines and trailing spaces on either fence: this strip fails OPEN — when it
  // does not match, the YAML ships to every session — so it must not be brittle about
  // whitespace an ordinary edit can introduce.
  const body = raw.replace(/^\s*---[ \t]*\r?\n[\s\S]*?\r?\n---[ \t]*\r?\n/, "").trim();

  if (!body) throw new Error("no body after front matter in " + SOURCE);
  if (/^\s*(name|description|keep-coding-instructions)\s*:/m.test(body.split("\n")[0])) {
    throw new Error("front matter survived the strip in " + SOURCE);
  }
  // Name the source. Without it these rules are indistinguishable from the user's own
  // instructions, and somebody wondering why their agent stopped re-checking has nothing to grep.
  process.stdout.write("From the agent-handbook plugin (standing rules):\n\n" + body + "\n");
} catch (err) {
  // Never fail a session over a standing rule: a silent absence is recoverable, a blocked
  // start is not. But say WHY on stderr, which --debug captures and stdout never sees, so a
  // broken path is distinguishable from a deliberate absence instead of looking identical.
  process.stderr.write("agent-handbook: standing rules not emitted: " + (err && err.message) + "\n");
}
