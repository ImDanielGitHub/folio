from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def insert_method_before(path: str, class_name: str, before_name: str, method: str) -> None:
    content = read(path)
    tree = ast.parse(content)
    owner = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    before = next(
        node
        for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == before_name
    )
    lines = content.splitlines(keepends=True)
    start = before.lineno - 1
    write(path, "".join(lines[:start]) + method.rstrip() + "\n\n" + "".join(lines[start:]))


ENCRYPTED_EXPORTS = '''"""Passphrase-encrypted portable Folio backup envelopes."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from finance_agent.storage import canonical_json

MAGIC = b"FOLIO-ENC-1\\n"
FORMAT = "folio.encrypted-backup@1"
MIN_PASSPHRASE_CHARACTERS = 12
MAX_PASSPHRASE_CHARACTERS = 1024
MAX_HEADER_BYTES = 16_384


def _validate_passphrase(passphrase: str) -> bytes:
    if not isinstance(passphrase, str):
        raise TypeError("passphrase must be text")
    if len(passphrase) < MIN_PASSPHRASE_CHARACTERS:
        raise ValueError(
            f"passphrase must contain at least {MIN_PASSPHRASE_CHARACTERS} characters"
        )
    if len(passphrase) > MAX_PASSPHRASE_CHARACTERS:
        raise ValueError("passphrase is too long")
    return passphrase.encode("utf-8")


def _derive_key(passphrase: bytes, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(passphrase)


@dataclass(frozen=True, slots=True)
class EncryptedBackupEnvelope:
    content: bytes
    sha256: str
    inner_archive_sha256: str
    backup_id: str
    workspace_id: str


def encrypt_backup(
    content: bytes,
    *,
    passphrase: str,
    backup_id: str,
    workspace_id: str,
) -> EncryptedBackupEnvelope:
    secret = _validate_passphrase(passphrase)
    salt = os.urandom(16)
    nonce = os.urandom(12)
    inner_sha256 = hashlib.sha256(content).hexdigest()
    header = {
        "format": FORMAT,
        "backupId": backup_id,
        "workspaceId": workspace_id,
        "innerArchiveSha256": inner_sha256,
        "cipher": "AES-256-GCM",
        "kdf": {
            "name": "scrypt",
            "n": 2**15,
            "r": 8,
            "p": 1,
            "salt": base64.b64encode(salt).decode("ascii"),
        },
        "nonce": base64.b64encode(nonce).decode("ascii"),
    }
    encoded_header = canonical_json(header).encode("utf-8")
    key = _derive_key(secret, salt)
    ciphertext = AESGCM(key).encrypt(nonce, content, encoded_header)
    envelope = MAGIC + struct.pack(">I", len(encoded_header)) + encoded_header + ciphertext
    return EncryptedBackupEnvelope(
        content=envelope,
        sha256=hashlib.sha256(envelope).hexdigest(),
        inner_archive_sha256=inner_sha256,
        backup_id=backup_id,
        workspace_id=workspace_id,
    )


def decrypt_backup(
    envelope: bytes,
    *,
    passphrase: str,
    expected_workspace_id: str | None = None,
) -> tuple[bytes, dict[str, object]]:
    secret = _validate_passphrase(passphrase)
    if not envelope.startswith(MAGIC) or len(envelope) < len(MAGIC) + 4:
        raise ValueError("encrypted backup has an invalid header")
    header_length = struct.unpack(">I", envelope[len(MAGIC): len(MAGIC) + 4])[0]
    if header_length <= 0 or header_length > MAX_HEADER_BYTES:
        raise ValueError("encrypted backup header length is invalid")
    header_start = len(MAGIC) + 4
    header_end = header_start + header_length
    if header_end >= len(envelope):
        raise ValueError("encrypted backup is truncated")
    encoded_header = envelope[header_start:header_end]
    try:
        header = json.loads(encoded_header)
    except json.JSONDecodeError as exc:
        raise ValueError("encrypted backup header is invalid") from exc
    if not isinstance(header, dict) or header.get("format") != FORMAT:
        raise ValueError("unsupported encrypted backup format")
    workspace_id = header.get("workspaceId")
    if expected_workspace_id is not None and workspace_id != expected_workspace_id:
        raise ValueError("encrypted backup belongs to a different workspace")
    kdf = header.get("kdf")
    if not isinstance(kdf, dict) or kdf.get("name") != "scrypt":
        raise ValueError("encrypted backup KDF is unsupported")
    if (kdf.get("n"), kdf.get("r"), kdf.get("p")) != (2**15, 8, 1):
        raise ValueError("encrypted backup KDF parameters are unsupported")
    try:
        salt = base64.b64decode(str(kdf["salt"]), validate=True)
        nonce = base64.b64decode(str(header["nonce"]), validate=True)
    except (KeyError, ValueError) as exc:
        raise ValueError("encrypted backup key metadata is invalid") from exc
    if len(salt) != 16 or len(nonce) != 12:
        raise ValueError("encrypted backup key metadata has invalid lengths")
    key = _derive_key(secret, salt)
    try:
        content = AESGCM(key).decrypt(nonce, envelope[header_end:], encoded_header)
    except InvalidTag as exc:
        raise ValueError("passphrase is incorrect or encrypted backup was modified") from exc
    expected_sha256 = header.get("innerArchiveSha256")
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ValueError("decrypted backup digest mismatch")
    return content, header
'''

IMPORT_METHOD = '''    def import_archive(
        self,
        content: bytes,
        *,
        workspace_id: str,
    ) -> WorkspaceBackup:
        archive_sha256 = _sha256(content)
        try:
            with zipfile.ZipFile(BytesIO(content), "r") as archive:
                if set(archive.namelist()) != {"manifest.json", "workspace.sqlite3"}:
                    raise ValueError("backup archive has an unexpected file set")
                manifest = json.loads(archive.read("manifest.json"))
                database_bytes = archive.read("workspace.sqlite3")
        except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
            raise ValueError("backup archive is invalid") from exc
        if manifest.get("format") != BACKUP_FORMAT:
            raise ValueError("unsupported backup format")
        if manifest.get("workspaceId") != workspace_id:
            raise ValueError("backup archive belongs to a different workspace")
        database_sha256 = _sha256(database_bytes)
        if manifest.get("databaseSha256") != database_sha256:
            raise ValueError("backup archive database digest mismatch")
        backup_id = str(manifest.get("backupId") or "")
        if not backup_id:
            raise ValueError("backup archive has no backup identifier")
        schema_version = int(manifest.get("schemaVersion", 0))
        supported_version = max(migration.version for migration in MIGRATIONS)
        if schema_version <= 0 or schema_version > supported_version:
            raise ValueError("backup schema version is unsupported")
        created_at = str(manifest.get("createdAt") or _now().isoformat())
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM workspace_backups WHERE backup_id = ?", (backup_id,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO workspace_backups(
                        backup_id, workspace_id, created_at, schema_version,
                        database_sha256, archive_sha256, manifest_json, content, size_bytes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        backup_id,
                        workspace_id,
                        created_at,
                        schema_version,
                        database_sha256,
                        archive_sha256,
                        canonical_json(manifest),
                        content,
                        len(content),
                    ),
                )
            elif str(existing["archive_sha256"]) != archive_sha256:
                raise ValueError("backup identifier is already bound to different content")
        return WorkspaceBackup(
            backup_id=backup_id,
            workspace_id=workspace_id,
            created_at=created_at,
            schema_version=schema_version,
            database_sha256=database_sha256,
            archive_sha256=archive_sha256,
            content=content,
        )
'''

SERVICE_METHODS = '''    async def encrypted_workspace_backup_payload(
        self,
        *,
        backup_id: str,
        passphrase: str,
    ) -> ArtifactPayload:
        backup = WorkspaceBackupManager(self.store).get(backup_id)
        envelope = await asyncio.to_thread(
            encrypt_backup,
            backup.content,
            passphrase=passphrase,
            backup_id=backup.backup_id,
            workspace_id=backup.workspace_id,
        )
        return ArtifactPayload(
            content=envelope.content,
            media_type="application/octet-stream",
            filename=f"folio-{backup.workspace_id}-{backup.backup_id}.folioenc",
            content_hash=envelope.sha256,
        )

    async def import_encrypted_workspace_backup(
        self,
        *,
        workspace_id: str,
        content: bytes,
        passphrase: str,
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID:
            raise KeyError(workspace_id)
        archive, header = await asyncio.to_thread(
            decrypt_backup,
            content,
            passphrase=passphrase,
            expected_workspace_id=workspace_id,
        )
        async with self._lock:
            backup = WorkspaceBackupManager(self.store).import_archive(
                archive, workspace_id=workspace_id
            )
        return {
            "backupId": backup.backup_id,
            "workspaceId": backup.workspace_id,
            "createdAt": backup.created_at,
            "schemaVersion": backup.schema_version,
            "databaseSha256": backup.database_sha256,
            "archiveSha256": backup.archive_sha256,
            "encryptedEnvelopeFormat": header["format"],
            "restored": False,
        }
'''

MODELS_AND_ROUTES = '''

class EncryptedExportRequest(RequestModel):
    passphrase: str = Field(min_length=12, max_length=1024)
'''

ROUTES = '''    @router.post("/v1/backups/{backup_id}/encrypted-export")
    async def encrypted_workspace_backup(
        backup_id: PathIdentifier,
        body: EncryptedExportRequest,
        services: Services,
    ) -> Response:
        try:
            value = await services.encrypted_workspace_backup_payload(
                backup_id=backup_id,
                passphrase=body.passphrase,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return Response(
            content=value.content,
            media_type=value.media_type,
            headers={
                "Content-Disposition": content_disposition(
                    value.filename, disposition="attachment"
                ),
                "ETag": f'"{value.content_hash}"',
                "Cache-Control": "no-store",
            },
        )

    @router.post("/v1/workspaces/{workspace_id}/encrypted-backups/import", status_code=201)
    async def import_encrypted_workspace_backup(
        workspace_id: PathIdentifier,
        services: Services,
        passphrase: Annotated[str, Form(min_length=12, max_length=1024)],
        file: Annotated[UploadFile, File()],
    ) -> dict[str, object]:
        try:
            content = await read_upload_with_limit(file, max_bytes=50_000_000)
            return dict(
                await services.import_encrypted_workspace_backup(
                    workspace_id=workspace_id,
                    content=content,
                    passphrase=passphrase,
                )
            )
        except UploadTooLarge as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

'''

TESTS = '''from __future__ import annotations

from pathlib import Path

import pytest

from finance_agent.api.services import LocalRouteServices
from finance_agent.storage.encrypted_exports import decrypt_backup, encrypt_backup


def test_encrypted_backup_round_trip_and_wrong_passphrase() -> None:
    content = b"synthetic folio backup bytes"
    envelope = encrypt_backup(
        content,
        passphrase="correct horse battery staple",
        backup_id="backup_test_123",
        workspace_id="ws_koru_studio",
    )
    assert envelope.content != content
    restored, header = decrypt_backup(
        envelope.content,
        passphrase="correct horse battery staple",
        expected_workspace_id="ws_koru_studio",
    )
    assert restored == content
    assert header["cipher"] == "AES-256-GCM"
    assert header["kdf"]["name"] == "scrypt"
    with pytest.raises(ValueError, match="incorrect|modified"):
        decrypt_backup(
            envelope.content,
            passphrase="wrong passphrase value",
            expected_workspace_id="ws_koru_studio",
        )


def test_encrypted_backup_detects_tampering() -> None:
    envelope = encrypt_backup(
        b"backup",
        passphrase="a sufficiently long passphrase",
        backup_id="backup_tamper_123",
        workspace_id="ws_koru_studio",
    )
    modified = envelope.content[:-1] + bytes([envelope.content[-1] ^ 1])
    with pytest.raises(ValueError, match="incorrect|modified"):
        decrypt_backup(
            modified,
            passphrase="a sufficiently long passphrase",
            expected_workspace_id="ws_koru_studio",
        )


@pytest.mark.asyncio
async def test_service_imports_encrypted_export_without_restoring_it(tmp_path: Path) -> None:
    services = LocalRouteServices(tmp_path / "folio.sqlite3", auto_seed=True)
    backup = await services.create_workspace_backup(workspace_id="ws_koru_studio")
    exported = await services.encrypted_workspace_backup_payload(
        backup_id=str(backup["backupId"]),
        passphrase="owner controlled passphrase",
    )
    imported = await services.import_encrypted_workspace_backup(
        workspace_id="ws_koru_studio",
        content=exported.content,
        passphrase="owner controlled passphrase",
    )
    assert imported["backupId"] == backup["backupId"]
    assert imported["restored"] is False
    assert services.workspace_snapshot_sync("ws_koru_studio")["snapshotId"]
    await services.aclose()
'''


def add_dependency() -> None:
    path = "services/api/pyproject.toml"
    content = read(path)
    marker = '  "fastapi>=0.116,<1",\n'
    if '"cryptography>=' not in content:
        if marker not in content:
            raise RuntimeError("dependency marker missing")
        content = content.replace(marker, '  "cryptography>=46,<47",\n' + marker, 1)
        write(path, content)


def add_module_and_backup_import() -> None:
    write("services/api/src/finance_agent/storage/encrypted_exports.py", ENCRYPTED_EXPORTS)
    path = "services/api/src/finance_agent/storage/backups.py"
    insert_method_before(path, "WorkspaceBackupManager", "restore", IMPORT_METHOD)


def update_service() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    marker = "from finance_agent.storage.backups import WorkspaceBackupManager\n"
    import_line = (
        "from finance_agent.storage.encrypted_exports import decrypt_backup, encrypt_backup\n"
    )
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("backup manager import missing")
        content = content.replace(marker, marker + import_line, 1)
        write(path, content)
    insert_method_before(path, "LocalRouteServices", "scheduler_settings", SERVICE_METHODS)


def update_protocol_and_routes() -> None:
    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def scheduler_settings(self) -> Mapping[str, object]: ...\n"
    addition = '''    async def encrypted_workspace_backup_payload(\n        self, *, backup_id: str, passphrase: str\n    ) -> ArtifactPayload: ...\n\n    async def import_encrypted_workspace_backup(\n        self, *, workspace_id: str, content: bytes, passphrase: str\n    ) -> Mapping[str, object]: ...\n\n'''
    if marker not in content:
        raise RuntimeError("scheduler protocol marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    import_marker = "from finance_agent.api.http_security import content_disposition\n"
    expanded = (
        "from finance_agent.api.http_security import (\n"
        "    UploadTooLarge,\n"
        "    content_disposition,\n"
        "    read_upload_with_limit,\n"
        ")\n"
    )
    if import_marker in content:
        content = content.replace(import_marker, expanded, 1)
    elif "read_upload_with_limit" not in content:
        raise RuntimeError("http security import changed")
    model_marker = "\n\nclass SchedulerSettingsRequest(RequestModel):"
    if model_marker not in content:
        raise RuntimeError("scheduler model marker missing")
    content = content.replace(model_marker, MODELS_AND_ROUTES + model_marker, 1)
    route_marker = '    @router.get("/v1/workspaces/{workspace_id}/scheduler")\n'
    if route_marker not in content:
        raise RuntimeError("scheduler route marker missing")
    content = content.replace(route_marker, ROUTES + route_marker, 1)
    write(path, content)


def raise_request_limit() -> None:
    path = "services/api/src/finance_agent/api/http_security.py"
    content = read(path)
    content = re.sub(
        r"MAX_REQUEST_BODY_BYTES: Final = \d+",
        "MAX_REQUEST_BODY_BYTES: Final = 55_000_000",
        content,
        count=1,
    )
    write(path, content)


def add_tests_and_docs() -> None:
    write("services/api/tests/storage/test_encrypted_exports.py", TESTS)
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 8: encrypted portable backup envelopes\n\n- Local backup archives can be exported through AES-256-GCM.\n- Keys are derived with scrypt using per-export random salt and authenticated header data.\n- Passphrases are bounded, never stored, and incorrect credentials fail closed.\n- Encrypted imports authenticate and decrypt before the existing archive validation path.\n- Importing creates a restore candidate but never silently replaces the active workspace.\n- Ordinary SQLite at-rest encryption and key recovery remain separate, explicitly unclaimed work.\n'''
    if "## Stack 8: encrypted portable backup envelopes" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_dependency()
    add_module_and_backup_import()
    update_service()
    update_protocol_and_routes()
    raise_request_limit()
    add_tests_and_docs()
    print("encrypted export changes applied")


if __name__ == "__main__":
    main()
