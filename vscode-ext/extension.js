// Claude Pet Launcher: when the desktop pet opens a project, it writes a
// request file naming the folder and the terminals to start. The VS Code
// window that has that folder open claims the request and opens the
// terminals in its own integrated terminal panel (Claude Code last, so it
// ends up focused).
const fs = require("fs");
const os = require("os");
const path = require("path");
const vscode = require("vscode");

const REQUEST = path.join(os.homedir(), ".claude-pet-vscode.json");
const MAX_AGE_MS = 300000; // ignore stale requests (e.g. VS Code opened much later)
const POLL_MS = 1000;

function norm(p) {
  return path.resolve(p).replace(/[\\/]+$/, "").toLowerCase();
}

function ownsFolder(folder) {
  const want = norm(folder);
  return (vscode.workspace.workspaceFolders || []).some(
    (f) => f.uri.scheme === "file" && norm(f.uri.fsPath) === want
  );
}

function tryClaim() {
  let request;
  try {
    request = JSON.parse(fs.readFileSync(REQUEST, "utf8"));
  } catch {
    return; // no request (or half-written; the next poll retries)
  }
  if (!request.folder || !ownsFolder(request.folder)) return;
  if (Date.now() - request.ts * 1000 > MAX_AGE_MS) return;
  // Rename is atomic, so only one window wins if two share the folder.
  const claimed = `${REQUEST}.${process.pid}`;
  try {
    fs.renameSync(REQUEST, claimed);
  } catch {
    return;
  }
  try {
    fs.unlinkSync(claimed);
  } catch {
    // leftover claim file is harmless
  }
  openTerminals(request.terminals || []);
}

function openTerminals(terminals) {
  let last;
  for (const t of terminals) {
    const term = vscode.window.createTerminal({
      name: t.name,
      cwd: t.cwd,
      shellPath: t.shell || undefined,
      iconPath: t.claude ? new vscode.ThemeIcon("sparkle") : undefined,
    });
    for (const line of t.commands || []) term.sendText(line, true);
    last = term;
  }
  if (last) last.show(false); // focus the last one (Claude)
}

function activate(context) {
  tryClaim();
  const timer = setInterval(tryClaim, POLL_MS);
  context.subscriptions.push({ dispose: () => clearInterval(timer) });
}

function deactivate() {}

module.exports = { activate, deactivate };
