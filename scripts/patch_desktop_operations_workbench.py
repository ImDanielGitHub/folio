from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def patch_service() -> None:
    path = ROOT / "services/api/src/finance_agent/api/services.py"
    content = path.read_text()
    marker = '''        sources = [
            {
                "sourceItemId": str(row["source_item_id"]),
                "sourceType": str(row["source_type"]),
                "label": str(row["label"]),
                "status": str(row["status"]),
                "rowCount": int(row["row_count"]),
                "receivedAt": str(row["received_at"]),
            }
            for row in self.store.fetch_all(
                "SELECT source_item_id, source_type, label, status, row_count, received_at FROM source_items WHERE workspace_id = ? ORDER BY received_at DESC, source_item_id",
                (workspace_id,),
            )
        ]
        return {
'''
    replacement = marker.replace(
        "        return {\n",
        '''        evidence_options = [
            {
                "evidenceId": str(row["evidence_id"]),
                "label": str(row["label"]),
                "createdAt": str(row["created_at"]),
            }
            for row in self.store.fetch_all(
                "SELECT evidence_id, label, created_at FROM evidence_links WHERE workspace_id = ? ORDER BY created_at DESC, evidence_id LIMIT 200",
                (workspace_id,),
            )
        ]
        return {
''',
    )
    if marker not in content:
        raise RuntimeError("operations summary source marker missing")
    content = content.replace(marker, replacement, 1)
    payload_marker = '            "sources": sources,\n'
    if payload_marker not in content:
        raise RuntimeError("operations summary sources payload missing")
    content = content.replace(
        payload_marker,
        payload_marker + '            "evidenceOptions": evidence_options,\n',
        1,
    )
    path.write_text(content)


def patch_types_component_test() -> None:
    path = ROOT / "apps/desktop/src/operations.ts"
    content = path.read_text()
    marker = '''  sources: Array<{
    sourceItemId: string;
    sourceType: string;
    label: string;
    status: string;
    rowCount: number;
    receivedAt: string;
  }>;
'''
    replacement = marker + '''  evidenceOptions: Array<{
    evidenceId: string;
    label: string;
    createdAt: string;
  }>;
'''
    if marker not in content:
        raise RuntimeError("operations summary sources type missing")
    path.write_text(content.replace(marker, replacement, 1))

    path = ROOT / "apps/desktop/src/OperationsWorkbench.tsx"
    content = path.read_text()
    content = content.replace(
        'function Field({ label, name, children }: { label: string; name?: string; children: React.ReactNode })',
        'function Field({ label, children }: { label: string; children: React.ReactNode })',
        1,
    )
    old = '''<Field label="Evidence"><select name="evidenceId" required>{summary?.sources.flatMap((source) => source.sourceItemId ? [<option key={source.sourceItemId} value={source.sourceItemId.replace(/^src_/, "evd_")}>{source.label}</option>] : [])}</select></Field>'''
    new = '''<Field label="Evidence"><select name="evidenceId" required>{summary?.evidenceOptions.map((evidence) => <option key={evidence.evidenceId} value={evidence.evidenceId}>{evidence.label}</option>)}</select></Field>'''
    if old not in content:
        raise RuntimeError("unsafe FX evidence derivation markup missing")
    path.write_text(content.replace(old, new, 1))

    path = ROOT / "services/api/tests/api/test_operations_summary.py"
    content = path.read_text()
    marker = '        assert value["sources"]\n'
    if marker not in content:
        raise RuntimeError("operations summary test source assertion missing")
    content = content.replace(
        marker,
        marker + '        assert value["evidenceOptions"]\n',
        1,
    )
    path.write_text(content)


if __name__ == "__main__":
    patch_service()
    patch_types_component_test()
    print("operations workbench evidence binding corrected")
