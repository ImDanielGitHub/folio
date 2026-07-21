import { app, BrowserWindow, dialog, ipcMain, shell } from "electron";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const currentDirectory = dirname(fileURLToPath(import.meta.url));
const apiBase = "http://127.0.0.1:8787";

function rendererUrl(): string | null {
  const argument = process.argv.find((value) => value.startsWith("--renderer-url="));
  return argument?.slice("--renderer-url=".length) ?? null;
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
      preload: join(currentDirectory, "..", "preload", "preload.cjs"),
    },
  });

  window.once("ready-to-show", () => {
    window.show();
    if (process.platform === "darwin") {
      window.setSimpleFullScreen(true);
    } else {
      window.setFullScreen(true);
    }
  });
  window.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith(`${apiBase}/v1/artifacts/`)) {
      void shell.openExternal(url);
    }
    return { action: "deny" };
  });
  window.webContents.on("will-navigate", (event, url) => {
    const allowed = rendererUrl();
    if (allowed && url.startsWith(allowed)) return;
    if (url.startsWith("file://")) return;
    event.preventDefault();
  });

  const developmentUrl = rendererUrl();
  if (developmentUrl) {
    await window.loadURL(developmentUrl);
  } else {
    await window.loadFile(join(currentDirectory, "..", "..", "dist", "index.html"));
  }
}

ipcMain.handle("finance:pick-csv", async () => {
  const result = await dialog.showOpenDialog({
    title: "Import a bank CSV",
    properties: ["openFile"],
    filters: [{ name: "CSV files", extensions: ["csv"] }],
  });
  if (result.canceled || !result.filePaths[0]) return null;
  const path = result.filePaths[0];
  const bytes = await readFile(path);
  return {
    name: path.split(/[\\/]/).at(-1) ?? "source.csv",
    base64: bytes.toString("base64"),
  };
});

ipcMain.handle("finance:open-artifact", async (_event, artifactId: string) => {
  if (!/^[a-z][a-z0-9_]{2,95}$/.test(artifactId)) return false;
  await shell.openExternal(`${apiBase}/v1/artifacts/${artifactId}`);
  return true;
});

app.whenReady().then(async () => {
  await createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) void createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
