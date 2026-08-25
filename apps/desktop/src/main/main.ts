import {
  app,
  BrowserWindow,
  dialog,
  ipcMain,
  net,
  protocol,
  session,
  shell,
} from "electron";
import { readFile, stat } from "node:fs/promises";
import { dirname, join, resolve, sep } from "node:path";
import { pathToFileURL, fileURLToPath } from "node:url";
import { isTrustedRendererUrl, isValidArtifactId, MAX_CSV_BYTES } from "./security.js";

protocol.registerSchemesAsPrivileged([
  {
    scheme: "app",
    privileges: {
      standard: true,
      secure: true,
      supportFetchAPI: true,
      corsEnabled: true,
    },
  },
]);

const currentDirectory = dirname(fileURLToPath(import.meta.url));
const apiBase = "http://127.0.0.1:8787";

function rendererUrl(): string | null {
  const argument = process.argv.find((value) => value.startsWith("--renderer-url="));
  return argument?.slice("--renderer-url=".length) ?? null;
}

function assertTrustedSender(url: string): void {
  if (!isTrustedRendererUrl(url, rendererUrl())) {
    throw new Error("Rejected IPC from an untrusted renderer origin")
  }
}

async function createWindow(): Promise<void> {
  const window = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 860,
    minHeight: 640,
    backgroundColor: "#0d0f0e",
    title: "Folio",
    autoHideMenuBar: true,
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      preload: join(currentDirectory, "..", "preload", "preload.cjs"),
    },
  });

  window.once("ready-to-show", () => window.show());
  window.webContents.setWindowOpenHandler(({ url }) => {
    const parsed = new URL(url);
    if (
      parsed.origin === apiBase
      && /^\/v1\/artifacts\/[a-z][a-z0-9]{1,15}_[a-z0-9][a-z0-9_]{2,95}$/.test(parsed.pathname)
    ) {
      void shell.openExternal(url);
    }
    return { action: "deny" };
  });
  window.webContents.on("will-navigate", (event, url) => {
    if (!isTrustedRendererUrl(url, rendererUrl())) event.preventDefault();
  });
  window.webContents.on("will-attach-webview", (event) => event.preventDefault());

  const developmentUrl = rendererUrl();
  if (developmentUrl) {
    await window.loadURL(developmentUrl);
  } else {
    await window.loadURL("app://folio/index.html");
  }
}

ipcMain.handle("finance:pick-csv", async (event) => {
  const senderUrl = event.senderFrame?.url;
  if (!senderUrl) {
    throw new Error("Rejected privileged IPC without a sender frame");
  }
  assertTrustedSender(senderUrl);
  const result = await dialog.showOpenDialog({
    title: "Import a bank CSV",
    properties: ["openFile"],
    filters: [{ name: "CSV files", extensions: ["csv"] }],
  });
  if (result.canceled || !result.filePaths[0]) return null;
  const path = result.filePaths[0];
  const metadata = await stat(path);
  if (!metadata.isFile() || metadata.size > MAX_CSV_BYTES) {
    throw new Error("CSV must be a regular file no larger than 10 MB")
  }
  const bytes = await readFile(path);
  return {
    name: path.split(/[\\/]/).at(-1) ?? "source.csv",
    base64: bytes.toString("base64"),
  };
});

ipcMain.handle("finance:open-artifact", async (event, artifactId: unknown) => {
  const senderUrl = event.senderFrame?.url;
  if (!senderUrl) {
    throw new Error("Rejected privileged IPC without a sender frame");
  }
  assertTrustedSender(senderUrl);
  if (!isValidArtifactId(artifactId)) return false;
  await shell.openExternal(`${apiBase}/v1/artifacts/${artifactId}`);
  return true;
});

app.whenReady().then(async () => {
  const rendererRoot = resolve(currentDirectory, "..", "..", "dist");
  protocol.handle("app", (request) => {
    const requestUrl = new URL(request.url);
    const relativePath = requestUrl.pathname === "/" ? "index.html" : requestUrl.pathname.slice(1);
    const resolvedPath = resolve(rendererRoot, relativePath);
    if (resolvedPath !== rendererRoot && !resolvedPath.startsWith(`${rendererRoot}${sep}`)) {
      return new Response("Not found", { status: 404 });
    }
    return net.fetch(pathToFileURL(resolvedPath).toString());
  });
  session.defaultSession.setPermissionCheckHandler(() => false);
  session.defaultSession.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false));
  await createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) void createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
