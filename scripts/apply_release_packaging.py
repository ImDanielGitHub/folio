from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


BUILDER = '''appId: nz.co.folio.finance
productName: Folio
copyright: Copyright © 2026 Daniel Aneke
asar: true
artifactName: ${productName}-${version}-${os}-${arch}.${ext}
directories:
  output: release
  buildResources: build
files:
  - dist/**
  - dist-electron/**
  - package.json
  - "!**/*.map"
  - "!**/*.test.*"
  - "!tests/**"
  - "!src/**"
extraMetadata:
  main: dist-electron/main/main.js
  description: Local-first finance operator for New Zealand small businesses
  license: Apache-2.0
mac:
  category: public.app-category.finance
  hardenedRuntime: true
  gatekeeperAssess: false
  entitlements: build/entitlements.mac.plist
  entitlementsInherit: build/entitlements.mac.plist
  target:
    - target: dmg
      arch: [arm64, x64]
    - target: zip
      arch: [arm64, x64]
win:
  target:
    - target: nsis
      arch: [x64]
    - target: portable
      arch: [x64]
nsis:
  oneClick: false
  perMachine: false
  allowToChangeInstallationDirectory: true
  createDesktopShortcut: true
  createStartMenuShortcut: true
linux:
  category: Office;Finance
  target:
    - target: AppImage
      arch: [x64]
    - target: deb
      arch: [x64]
  executableName: folio
publish: null
'''

ENTITLEMENTS = '''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>com.apple.security.cs.allow-jit</key>
  <true/>
  <key>com.apple.security.cs.allow-unsigned-executable-memory</key>
  <true/>
  <key>com.apple.security.cs.disable-library-validation</key>
  <true/>
</dict>
</plist>
'''

PACKAGE_SMOKE = '''import { access, readFile, readdir, stat } from "node:fs/promises";
import { constants } from "node:fs";
import { join } from "node:path";
import process from "node:process";

const root = new URL("..", import.meta.url).pathname;
const release = join(root, "apps", "desktop", "release");

async function walk(path) {
  const entries = await readdir(path, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const target = join(path, entry.name);
    if (entry.isDirectory()) files.push(...await walk(target));
    else files.push(target);
  }
  return files;
}

await access(release, constants.R_OK);
const files = await walk(release);
const appAsar = files.find((file) => file.endsWith("resources/app.asar"));
if (!appAsar) throw new Error("packaged app.asar was not produced");
const asarStat = await stat(appAsar);
if (asarStat.size < 1000) throw new Error("packaged app.asar is unexpectedly small");
const sourceMap = files.find((file) => file.endsWith(".map"));
if (sourceMap) throw new Error(`source map leaked into release output: ${sourceMap}`);
const packageJson = JSON.parse(await readFile(join(root, "apps", "desktop", "package.json"), "utf8"));
if (!/^\\d+\\.\\d+\\.\\d+$/.test(packageJson.version)) {
  throw new Error("desktop package version must be semantic x.y.z");
}
const executables = files.filter((file) => /(?:\\.exe|\\.AppImage|\\/folio)$/.test(file));
if (executables.length === 0) throw new Error("no packaged executable was found");
console.log(JSON.stringify({
  status: "PASS",
  version: packageJson.version,
  appAsarBytes: asarStat.size,
  executableCount: executables.length,
  signed: false,
  notarised: false,
}, null, 2));
'''

RELEASE_WORKFLOW = '''name: Package release candidates

on:
  workflow_dispatch:
    inputs:
      version:
        description: Expected semantic version
        required: true
        type: string
  push:
    tags:
      - "v*.*.*"

permissions:
  contents: read

concurrency:
  group: package-${{ github.ref }}
  cancel-in-progress: false

jobs:
  package:
    strategy:
      fail-fast: false
      matrix:
        include:
          - os: macos-14
            target: mac
          - os: windows-latest
            target: win
          - os: ubuntu-latest
            target: linux
    runs-on: ${{ matrix.os }}
    timeout-minutes: 60
    env:
      CSC_LINK: ${{ secrets.CSC_LINK }}
      CSC_KEY_PASSWORD: ${{ secrets.CSC_KEY_PASSWORD }}
      APPLE_ID: ${{ secrets.APPLE_ID }}
      APPLE_APP_SPECIFIC_PASSWORD: ${{ secrets.APPLE_APP_SPECIFIC_PASSWORD }}
      APPLE_TEAM_ID: ${{ secrets.APPLE_TEAM_ID }}
    steps:
      - uses: actions/checkout@v4
      - uses: pnpm/action-setup@v4
        with:
          version: 10.33.0
          run_install: false
      - uses: actions/setup-node@v4
        with:
          node-version: 22
          cache: pnpm
          cache-dependency-path: pnpm-lock.yaml
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - uses: astral-sh/setup-uv@v6
        with:
          version: "0.10.0"
          enable-cache: true
      - run: pnpm install --frozen-lockfile
      - run: uv sync --project services/api --frozen
      - run: pnpm verify
      - name: Validate requested version
        shell: bash
        run: |
          actual="$(node -p "require('./apps/desktop/package.json').version")"
          expected="${{ inputs.version || github.ref_name }}"
          expected="${expected#v}"
          test "$actual" = "$expected"
      - name: Build platform packages without publishing
        run: pnpm --filter @folio/desktop dist:${{ matrix.target }}
      - name: Write proof receipt
        shell: bash
        run: |
          signed=false
          notarised=false
          if [ -n "${CSC_LINK:-}" ]; then signed=true; fi
          if [ "${{ matrix.target }}" = mac ] && [ -n "${APPLE_ID:-}" ] && [ -n "${APPLE_APP_SPECIFIC_PASSWORD:-}" ] && [ -n "${APPLE_TEAM_ID:-}" ]; then notarised=true; fi
          node -e 'const fs=require("fs"); fs.writeFileSync("apps/desktop/release/proof.json", JSON.stringify({commit:process.env.GITHUB_SHA,platform:process.argv[1],signed:process.argv[2]==="true",notarised:process.argv[3]==="true",published:false},null,2))' "${{ matrix.target }}" "$signed" "$notarised"
      - uses: actions/upload-artifact@v4
        with:
          name: folio-${{ matrix.target }}-${{ github.sha }}
          path: apps/desktop/release/**
          if-no-files-found: error
          retention-days: 14
'''

RELEASE_DOC = '''# Release packaging and proof boundary

Folio uses Electron Builder to create macOS, Windows and Linux release candidates. The package configuration excludes source maps, tests and source files from the application archive. Local and pull-request verification proves source, tests and a Linux unpacked package only when the corresponding commands run successfully.

The permanent package workflow runs on an explicit workflow dispatch or semantic version tag. It verifies the repository before packaging and uploads platform artefacts without publishing a GitHub release. `proof.json` records the commit, platform and whether signing/notarisation credentials were actually present.

A generated DMG, ZIP, NSIS installer, portable EXE, AppImage or DEB is not evidence that it is signed, notarised, installed successfully or distributed. Signing and Apple notarisation depend on repository secrets owned by Daniel. Store review, auto-update rollout, clean-machine installation and upgrade/downgrade acceptance require separate observed evidence.
'''


def add_builder_config_and_scripts() -> None:
    write("apps/desktop/electron-builder.yml", BUILDER)
    write("apps/desktop/build/entitlements.mac.plist", ENTITLEMENTS)
    write("scripts/package_smoke.mjs", PACKAGE_SMOKE)
    write(".github/workflows/release.yml", RELEASE_WORKFLOW)
    write("docs/RELEASE_PACKAGING.md", RELEASE_DOC)

    path = "apps/desktop/package.json"
    value = json.loads(read(path))
    value["version"] = "0.2.0"
    dev = value["devDependencies"]
    dev["electron-builder"] = "^26.0.0"
    scripts = value["scripts"]
    scripts["package:dir"] = "pnpm build && electron-builder --dir --config electron-builder.yml --linux dir"
    scripts["dist:mac"] = "pnpm build && electron-builder --config electron-builder.yml --mac --publish never"
    scripts["dist:win"] = "pnpm build && electron-builder --config electron-builder.yml --win --publish never"
    scripts["dist:linux"] = "pnpm build && electron-builder --config electron-builder.yml --linux --publish never"
    write(path, json.dumps(value, indent=2) + "\n")


def add_root_gate() -> None:
    path = "package.json"
    value = json.loads(read(path))
    value["version"] = "0.2.0"
    scripts = value["scripts"]
    scripts["package:smoke"] = "pnpm --filter @folio/desktop package:dir && node scripts/package_smoke.mjs"
    write(path, json.dumps(value, indent=2) + "\n")

    path = "services/api/pyproject.toml"
    content = read(path).replace('version = "0.0.0"', 'version = "0.2.0"', 1)
    write(path, content)


def update_docs() -> None:
    path = "README.md"
    content = read(path)
    addition = '''\n### Build an unpacked release candidate\n\n```bash\npnpm package:smoke\n```\n\nThis proves a Linux unpacked package can be assembled and inspected. It does not prove code signing, notarisation, installation or publication. See [docs/RELEASE_PACKAGING.md](docs/RELEASE_PACKAGING.md).\n'''
    if "### Build an unpacked release candidate" not in content:
        marker = "\n## Model modes and privacy\n"
        if marker not in content:
            raise RuntimeError("README model modes marker missing")
        content = content.replace(marker, addition + marker, 1)
        write(path, content)

    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 14: reproducible desktop release packaging\n\n- Electron Builder defines macOS, Windows and Linux artefacts from the same tested source tree.\n- Packaged application archives exclude source maps, tests and source files.\n- A Linux unpacked-package smoke gate verifies app.asar and an executable before merge.\n- The permanent matrix workflow packages all three platforms without publishing.\n- Signing and notarisation are conditional on owner-controlled secrets and are recorded truthfully in `proof.json`.\n- Installer execution, signing, notarisation, clean-machine acceptance, auto-update and release publication remain separate proof levels.\n'''
    if "## Stack 14: reproducible desktop release packaging" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_builder_config_and_scripts()
    add_root_gate()
    update_docs()
    print("release packaging changes applied")


if __name__ == "__main__":
    main()
