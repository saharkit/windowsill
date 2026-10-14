#!/usr/bin/env node

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const SETTINGS_DIR = path.join(require('node:os').homedir(), '.claude');
const SETTINGS_PATH = path.join(SETTINGS_DIR, 'settings.json');
const MAX_TEMP_ATTEMPTS = 100;

function fail(message) {
  process.stderr.write(`agent-statusline: ${message}\n`);
  process.exitCode = 1;
}

function backupPath() {
  const stamp = new Date().toISOString().replace(/[-:]/g, '').replace('.', '');
  const base = path.join(SETTINGS_DIR, `settings.json.bak.${stamp}`);
  for (let suffix = 0; ; suffix += 1) {
    const candidate = suffix === 0 ? base : `${base}.${suffix}`;
    try {
      const fd = fs.openSync(candidate, 'wx');
      return { fd, path: candidate };
    } catch (error) {
      if (error.code !== 'EEXIST') throw error;
    }
  }
}

function makeTemp(content) {
  for (let attempt = 0; attempt < MAX_TEMP_ATTEMPTS; attempt += 1) {
    const suffix = crypto.randomBytes(16).toString('hex');
    const tempPath = path.join(SETTINGS_DIR, `.settings.json.tmp-${process.pid}-${suffix}`);
    let fd;
    try {
      fd = fs.openSync(tempPath, 'wx');
    } catch (error) {
      if (error.code === 'EEXIST') continue;
      throw error;
    }
    try {
      fs.writeFileSync(fd, content, 'utf8');
      fs.fsyncSync(fd);
      fs.closeSync(fd);
      return { path: tempPath };
    } catch (error) {
      try { fs.closeSync(fd); } catch (_) { /* already closed */ }
      try { fs.unlinkSync(tempPath); } catch (_) { /* best effort cleanup */ }
      throw error;
    }
  }
  throw new Error(`temporary file collision exhaustion after ${MAX_TEMP_ATTEMPTS} attempts`);
}

function writeSettings(value) {
  const content = `${JSON.stringify(value, null, 2)}\n`;
  const temp = makeTemp(content);
  try {
    fs.renameSync(temp.path, SETTINGS_PATH);
  } catch (error) {
    try { fs.unlinkSync(temp.path); } catch (_) { /* only our temp is ours to remove */ }
    throw error;
  }
}

function main() {
  const args = process.argv.slice(2);
  if (args.length === 0 || (args[0] !== 'set' && args[0] !== 'remove') ||
      (args[0] === 'set' && args.length !== 2) || (args[0] === 'remove' && args.length !== 1)) {
    throw new Error('usage: settings-edit.js set <absolute-renderer-path> | remove');
  }
  const command = args[0];
  if (command === 'set' && !path.isAbsolute(args[1])) {
    throw new Error('set requires an absolute renderer path');
  }

  let original;
  try {
    original = fs.readFileSync(SETTINGS_PATH, 'utf8');
  } catch (error) {
    if (error.code === 'ENOENT') original = null;
    else throw error;
  }
  if (original === null && command === 'remove') return;

  let settings = {};
  if (original !== null) {
    try {
      settings = JSON.parse(original);
    } catch (error) {
      throw new Error(`settings.json is not valid JSON: ${error.message}`);
    }
    if (settings === null || typeof settings !== 'object' || Array.isArray(settings)) {
      throw new Error('settings.json must contain a JSON object');
    }
  }

  const changesSettings = command === 'set' ||
    Object.prototype.hasOwnProperty.call(settings, 'statusLine');
  if (!changesSettings) return;

  let backup = null;
  if (original !== null) {
    backup = backupPath();
    try {
      fs.writeFileSync(backup.fd, original, 'utf8');
      fs.fsyncSync(backup.fd);
      fs.closeSync(backup.fd);
    } catch (error) {
      try { fs.closeSync(backup.fd); } catch (_) { /* already closed */ }
      try { fs.unlinkSync(backup.path); } catch (_) { /* only our backup is ours to remove */ }
      throw error;
    }
  }

  if (command === 'set') {
    settings.statusLine = { type: 'command', command: `node ${args[1]}` };
    writeSettings(settings);
  } else if (Object.prototype.hasOwnProperty.call(settings, 'statusLine')) {
    delete settings.statusLine;
    writeSettings(settings);
  }
}

try {
  main();
} catch (error) {
  fail(error && error.message ? error.message : String(error));
}
