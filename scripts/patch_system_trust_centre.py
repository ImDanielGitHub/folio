from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def patch_integrity() -> None:
    path = ROOT / "services/api/src/finance_agent/storage/integrity.py"
    content = path.read_text()
    content = content.replace(
        '''            SELECT r.row_number, r.raw_json, r.row_hash, s.digest
            FROM source_rows r JOIN source_items s ON s.source_item_id = r.source_item_id
''',
        '''            SELECT r.row_number, r.raw_json, r.row_hash, r.mapping_version, s.digest
            FROM source_rows r JOIN source_items s ON s.source_item_id = r.source_item_id
''',
        1,
    )
    old = '''        for row in rows:
            expected = _sha(
                f"{row['digest']}\\0{row['row_number']}\\0{row['raw_json']}"
            )
            mismatches += expected != str(row["row_hash"])
'''
    new = '''        for row in rows:
            mapping_version = str(row["mapping_version"])
            if mapping_version.startswith("fx_conversion@"):
                expected = _sha(str(row["raw_json"]))
            else:
                expected = _sha(
                    f"{row['digest']}\\0{row['row_number']}\\0{row['raw_json']}"
                )
            mismatches += expected != str(row["row_hash"])
'''
    if old not in content:
        raise RuntimeError("source-row hash verification block missing")
    path.write_text(content.replace(old, new, 1))


if __name__ == "__main__":
    patch_integrity()
    print("integrity mapping-aware row hash verification applied")
