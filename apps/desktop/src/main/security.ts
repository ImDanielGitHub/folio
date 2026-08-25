export const MAX_CSV_BYTES = 10_000_000;

export function isTrustedRendererUrl(candidate: string, developmentUrl?: string | null): boolean {
  let value: URL;
  try {
    value = new URL(candidate);
  } catch {
    return false;
  }
  if (value.protocol === "app:" && value.hostname === "folio") return true;
  if (!developmentUrl) return false;
  try {
    const development = new URL(developmentUrl);
    return value.origin === development.origin;
  } catch {
    return false;
  }
}

export function isValidArtifactId(value: unknown): value is string {
  return typeof value === "string" && /^[a-z][a-z0-9]{1,15}_[a-z0-9][a-z0-9_]{2,95}$/.test(value);
}
