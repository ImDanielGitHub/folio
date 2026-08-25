import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("financeDesktop", {
  runtime: "electron",
  apiBase: "http://127.0.0.1:8787",
  sessionToken: process.env.FOLIO_SESSION_TOKEN || undefined,
  pickCsv: () => ipcRenderer.invoke("finance:pick-csv"),
  openArtifact: (artifactId: string) => ipcRenderer.invoke("finance:open-artifact", artifactId),
});
