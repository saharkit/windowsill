#!/usr/bin/env node
// Emits the handbook's standing rules on SessionStart. Its stdout becomes context the
// model sees for the session, and it re-fires on compact and resume.
//
// Written exec-form on purpose. The obvious shell form — {"command": "echo 'RULE: ...'"} —
// is POSIX-only: cmd.exe does not strip single quotes, so on Windows the rule arrives
// wrapped in literal quotes. That is worse than a crash, because it looks like it worked.
//
// One source of truth: the text lives in the output style beside this file, so the two
// delivery paths can never drift apart.

const fs = require("fs");
const path = require("path");

const SOURCE = path.join(__dirname, "..", "output-styles", "flagellant-rule.md");

try {
  const raw = fs.readFileSync(SOURCE, "utf8");
  // Strip the YAML front matter; it addresses the output-style loader, not the reader.
  const body = raw.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n/, "").trim();
  if (body) process.stdout.write(body + "\n");
} catch {
  // A standing rule that cannot be read is not worth failing a session over. Say nothing
  // and let the session start: a silent absence is recoverable, a blocked start is not.
}
