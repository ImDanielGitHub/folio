/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_FINANCE_API_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

interface Window {
  financeDesktop?: {
    runtime: "electron";
    apiBase: string;
    pickCsv: () => Promise<{ name: string; base64: string } | null>;
    openArtifact: (artifactId: string) => Promise<boolean>;
  };
}
