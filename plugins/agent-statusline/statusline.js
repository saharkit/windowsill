#!/usr/bin/env node
// agent-statusline: render one line for Claude Code's status line.
//
// Reads the harness's statusline payload from stdin (a JSON document whose shape is documented
// by the harness) and prints one line to stdout with three figures — context use, weekly budget
// (when present) and the active model. Every figure is read from the payload; the script makes
// no network calls and reads no host state. Targets Node >= 18.
//
// Off the happy path: an unparseable or empty stdin prints nothing to stdout, prints one line
// naming the parse failure to stderr, and exits 1. The happy path exits 0.

'use strict';

const RESET = '\x1b[0m';
const DIM = '\x1b[2m';
const RED = '\x1b[31m';
const YELLOW = '\x1b[33m';
const GREEN = '\x1b[32m';
const GLYPH = '◐';

function colorForRemainingPct(remaining) {
  if (remaining > 50) return GREEN;
  if (remaining >= 20) return YELLOW;
  return RED;
}

function colorForWeeklyPct(weekly) {
  if (weekly > 85) return RED;
  if (weekly > 60) return YELLOW;
  return GREEN;
}

function lastPathSegment(value) {
  if (typeof value !== 'string' || value.length === 0) return '?';
  const parts = value.split('/');
  return parts[parts.length - 1] || value;
}

function build(input) {
  const payload = JSON.parse(input);
  const modelName = (payload.model && payload.model.display_name) || '?';
  const size = (payload.context_window && payload.context_window.context_window_size) || 0;
  const used = (payload.context_window && payload.context_window.total_input_tokens) || 0;
  const usedPct = payload.context_window ? payload.context_window.used_percentage : null;
  const weekly = payload.rate_limits ? payload.rate_limits.seven_day : null;
  const weeklyPct = weekly ? weekly.used_percentage : null;
  const fiveHour = payload.rate_limits ? payload.rate_limits.five_hour : null;
  const fiveHourPct = fiveHour ? fiveHour.used_percentage : null;
  const cwd = (payload.workspace && payload.workspace.current_dir) || payload.cwd || '?';

  let budget = '';
  if (weeklyPct !== null && weeklyPct !== undefined) {
    const wk = Math.floor(weeklyPct);
    const wkColor = colorForWeeklyPct(weeklyPct);
    let hourSuffix = '';
    if (fiveHourPct !== null && fiveHourPct !== undefined) {
      const hr = Math.floor(fiveHourPct);
      hourSuffix = DIM + '/5h ' + hr + '%' + RESET;
    }
    budget = wkColor + ' wk ' + wk + '%' + RESET + hourSuffix;
  }

  if (size > 0 && usedPct !== null && usedPct !== undefined) {
    const upi = Math.floor(usedPct);
    const remaining = 100 - upi;
    const col = colorForRemainingPct(remaining);
    const usedK = Math.floor(used / 1000);
    const sizeK = Math.floor(size / 1000);
    return (
      col + GLYPH + ' ctx ' + usedK + 'k/' + sizeK + 'k (' + upi + '%)' + RESET +
      (budget ? budget + ' ' : ' ') +
      DIM + modelName + RESET
    );
  }

  return DIM + modelName + RESET + (budget ? budget + ' ' : ' ') + lastPathSegment(cwd);
}

function main() {
  let raw = '';
  const chunks = [];
  process.stdin.on('data', (c) => chunks.push(c));
  process.stdin.on('end', () => {
    raw = Buffer.concat(chunks).toString('utf8');
    if (raw.trim().length === 0) {
      process.stderr.write('agent-statusline: stdin was empty\n');
      process.exit(1);
      return;
    }
    let line;
    try {
      line = build(raw);
    } catch (err) {
      process.stderr.write('agent-statusline: failed to parse stdin as JSON: ' + (err && err.message ? err.message : String(err)) + '\n');
      process.exit(1);
      return;
    }
    process.stdout.write(line + '\n');
  });
}

main();
