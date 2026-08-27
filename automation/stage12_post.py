from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def replace_once(path: str, old: str, new: str, label: str) -> None:
    file = ROOT / path
    value = file.read_text(encoding="utf-8")
    count = value.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    file.write_text(value.replace(old, new, 1), encoding="utf-8")


def main() -> None:
    replace_once(
        "apps/desktop/electron-builder.yml",
        '''extraResources:\n  - from: ../../dist-sidecar/folio-api\n    to: sidecar/folio-api\n    filter:\n      - "**/*"\n  - from: ../../dist-sidecar/folio-api.exe\n    to: sidecar/folio-api.exe\n    filter:\n      - "**/*"\n  - from: ../../dist-sidecar/release-manifest.json\n''',
        '''extraResources:\n  - from: ../../dist-sidecar\n    to: sidecar\n    filter:\n      - folio-api\n      - folio-api.exe\n  - from: ../../dist-sidecar/release-manifest.json\n''',
        "platform-neutral sidecar resource",
    )
    replace_once(
        "apps/desktop/src/main/main.ts",
        '''      FOLIO_SESSION_TOKEN: sessionToken,\n      FOLIO_RESOURCE_ROOT: process.resourcesPath,\n    },\n''',
        '''      FOLIO_SESSION_TOKEN: sessionToken,\n    },\n''',
        "PyInstaller resource root",
    )
    replace_once(
        "apps/desktop/src/main/main.ts",
        '''app.whenReady().then(async () => {\n  await startPackagedApi();\n''',
        '''app.whenReady().then(async () => {\n  await startPackagedApi();\n''',
        "packaged startup anchor",
    )
    path = ROOT / "apps/desktop/src/main/main.ts"
    value = path.read_text(encoding="utf-8")
    old = '''  app.on("activate", () => {\n    if (BrowserWindow.getAllWindows().length === 0) void createWindow();\n  });\n});\n\napp.on("before-quit", () => {\n'''
    new = '''  app.on("activate", () => {\n    if (BrowserWindow.getAllWindows().length === 0) void createWindow();\n  });\n}).catch(async (error: unknown) => {\n  const message = error instanceof Error ? error.message : "Unknown packaged startup failure";\n  if (process.env.FOLIO_SMOKE_MARKER) {\n    await writeFile(\n      process.env.FOLIO_SMOKE_MARKER,\n      `${JSON.stringify({ apiReady: false, windowReady: false, error: message.slice(0, 400) })}\\n`,\n      { mode: 0o600 },\n    );\n  }\n  app.exit(1);\n});\n\napp.on("before-quit", () => {\n'''
    count = value.count(old)
    if count != 1:
        raise RuntimeError(f"packaged startup error handling: expected one match, found {count}")
    path.write_text(value.replace(old, new, 1), encoding="utf-8")


if __name__ == "__main__":
    main()
