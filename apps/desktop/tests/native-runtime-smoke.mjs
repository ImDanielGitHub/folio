/** Launch the real packaged-origin app against a disposable, synthetic local API. */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:net";
import { createRequire } from "node:module";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { resolve, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const desktop = fileURLToPath(new URL("../", import.meta.url));
const temporary = await mkdtemp(join(process.env.RUNNER_TEMP || tmpdir(), "folio-native-"));
const evidence = process.env.FOLIO_SMOKE_EVIDENCE || join(temporary, "receipt.json");
const token = "synthetic-native-smoke-session";
const environment = {
  ...process.env, FOLIO_RUNTIME_MODE: "demo", FINANCE_DATABASE_PATH: join(temporary, "workspace.sqlite3"),
  FOLIO_SESSION_TOKEN: token, OPENAI_API_KEY: "", LM_STUDIO_BASE_URL: "http://127.0.0.1:65530/v1",
  LM_STUDIO_API_TOKEN: "", FINANCE_AKAHU_ENABLED: "false", FINANCE_PLAID_ENABLED: "false",
};
const delay = (ms) => new Promise((resolveDelay) => setTimeout(resolveDelay, ms));
async function waitFor(work, label) {
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    try { const value = await work(); if (value) return value; } catch { /* still starting */ }
    await delay(200);
  }
  throw new Error(`Timed out waiting for ${label}`);
}
async function freePort() {
  const server = createServer();
  await new Promise((done, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", done); });
  const port = server.address().port;
  await new Promise((done) => server.close(done));
  return port;
}
function launch(command, args, cwd) {
  const child = spawn(command, args, { cwd, env: environment, stdio: ["ignore", "pipe", "pipe"] });
  // Drain output without dumping potentially sensitive environment or request details.
  child.stdout.on("data", () => {});
  child.stderr.on("data", () => {});
  child.on("error", (error) => { child.launchError = error.message; });
  return child;
}
async function stop(child) {
  if (!child || child.exitCode !== null) return;
  child.kill("SIGTERM");
  for (let attempt = 0; attempt < 30 && child.exitCode === null; attempt++) await delay(100);
  if (child.exitCode === null) child.kill("SIGKILL");
}
let api, electron, socket;
let sequence = 0;
const pending = new Map();
function rpc(method, params = {}) {
  const id = ++sequence;
  return new Promise((done, reject) => {
    const timeout = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 30_000);
    pending.set(id, { done, reject, timeout });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression) {
  const result = await rpc("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (result.exceptionDetails) throw new Error("Native renderer evaluation failed");
  return result.result.value;
}
try {
  api = launch("uv", ["run", "--project", "services/api", "uvicorn", "finance_agent.api.app:app", "--app-dir", "services/api/src", "--host", "127.0.0.1", "--port", "8787"], root);
  await waitFor(async () => (await fetch("http://127.0.0.1:8787/health")).ok, "local API");
  const port = await freePort();
  const binary = createRequire(import.meta.url)("electron");
  electron = launch(binary, [resolve(desktop, "dist-electron/main/main.js"), `--remote-debugging-port=${port}`], desktop);
  const target = await waitFor(async () => {
    const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    return targets.find((item) => item.type === "page" && item.url.startsWith("app://folio"));
  }, "Electron application page");
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((done, reject) => { socket.addEventListener("open", done, { once: true }); socket.addEventListener("error", reject, { once: true }); });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    const call = pending.get(message.id);
    if (!call) return;
    clearTimeout(call.timeout); pending.delete(message.id);
    if (message.error) call.reject(new Error(message.error.message)); else call.done(message.result);
  });
  await waitFor(() => evaluate('Boolean(window.financeDesktop && document.querySelector("#root")?.textContent?.includes("Folio"))'), "rendered Folio UI and preload");
  const state = await evaluate(`(async () => {
    const headers = { "X-Folio-Session": window.financeDesktop.sessionToken };
    const snapshot = await fetch(window.financeDesktop.apiBase + "/v1/workspaces/ws_koru_studio/snapshot", { headers });
    const value = await snapshot.json();
    return { url: location.href, runtime: window.financeDesktop.runtime, requireType: typeof window.require,
      status: snapshot.status, workspace: value.workspace?.workspaceId,
      pdfOpened: await window.financeDesktop.openArtifact("artifact_koru_owner_pack_pdf") };
  })()`);
  assert.equal(state.url, "app://folio/index.html");
  assert.equal(state.runtime, "electron");
  assert.equal(state.requireType, "undefined");
  assert.equal(state.status, 200);
  assert.equal(state.workspace, "ws_koru_studio");
  assert.equal(state.pdfOpened, true);
  const screenshot = await rpc("Page.captureScreenshot", { format: "png" });
  await writeFile(`${evidence}.png`, Buffer.from(screenshot.data, "base64"));
  await writeFile(evidence, JSON.stringify({ status: "passed", platform: process.platform, ...state }, null, 2));
  console.log("Native smoke passed: packaged origin, preload isolation, authenticated API and actual PDF-opening IPC.");
} finally {
  for (const call of pending.values()) { clearTimeout(call.timeout); call.reject(new Error("Native smoke stopped")); }
  socket?.close();
  await stop(electron);
  await stop(api);
}
