import { spawn } from "node:child_process";

const children = [
  spawn("uv", ["run", "--project", "services/api", "uvicorn", "finance_agent.api.app:app", "--host", "127.0.0.1", "--port", "8787"], {
    stdio: "inherit",
  }),
  spawn("pnpm", ["--filter", "@folio/desktop", "dev:browser"], {
    stdio: "inherit",
  }),
];

let stopping = false;
function stop(signal = "SIGTERM") {
  if (stopping) return;
  stopping = true;
  for (const child of children) child.kill(signal);
}

for (const child of children) {
  child.on("error", (error) => {
    console.error(`Folio development process failed to start: ${error.message}`);
    stop();
    process.exitCode = 1;
  });
  child.on("exit", (code, signal) => {
    if (stopping) return;
    console.error(`Folio development process exited (${signal ?? code ?? "unknown"}).`);
    stop();
    process.exitCode = code ?? 1;
  });
}

process.on("SIGINT", () => stop("SIGINT"));
process.on("SIGTERM", () => stop("SIGTERM"));
