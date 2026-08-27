/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly DEV: boolean;
  readonly VITE_FINANCE_API_URL?: string;
  readonly VITE_FOLIO_SESSION_TOKEN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

interface Window {
  financeDesktop?: {
    runtime: "electron";
    apiBase: string;
    sessionToken?: string;
    pickCsv: () => Promise<{ name: string; base64: string } | null>;
    openArtifact: (artifactId: string) => Promise<boolean>;
  };
}
