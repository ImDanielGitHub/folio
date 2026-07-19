export function formatMoney(valueMinor: number, currency = "NZD", compact = false): string {
  return new Intl.NumberFormat("en-NZ", {
    style: "currency",
    currency,
    minimumFractionDigits: compact && valueMinor % 100 === 0 ? 0 : 2,
    maximumFractionDigits: 2,
  }).format(valueMinor / 100);
}

export function formatDateTime(value: string): string {
  return new Intl.DateTimeFormat("en-NZ", {
    day: "numeric",
    month: "short",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(value));
}

export function formatDate(value: string): string {
  const date = new Date(`${value}T00:00:00`);
  return new Intl.DateTimeFormat("en-NZ", { day: "numeric", month: "short" }).format(date);
}

export function titleCase(value: string | null): string {
  if (!value) return "—";
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function shortHash(value: string): string {
  return `${value.slice(0, 8)}…${value.slice(-6)}`;
}
