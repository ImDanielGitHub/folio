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
        "services/api/src/finance_agent/privacy/data_control.py",
        '''            for table in self.INVENTORY_TABLES:\n                if table not in available:\n                    counts[table] = 0\n                    continue\n                row = connection.execute(\n                    f'SELECT COUNT(*) AS count FROM "{table}" WHERE workspace_id = ?',\n                    (workspace_id,),\n                ).fetchone()\n                counts[table] = int(row["count"])\n''',
        '''            for table in self.INVENTORY_TABLES:\n                if table not in available:\n                    counts[table] = 0\n                    continue\n                if table == "source_rows":\n                    row = connection.execute(\n                        """\n                        SELECT COUNT(*) AS count FROM source_rows\n                        JOIN source_items USING (source_item_id)\n                        WHERE source_items.workspace_id = ?\n                        """,\n                        (workspace_id,),\n                    ).fetchone()\n                else:\n                    row = connection.execute(\n                        f'SELECT COUNT(*) AS count FROM "{table}" WHERE workspace_id = ?',\n                        (workspace_id,),\n                    ).fetchone()\n                counts[table] = int(row["count"])\n''',
        "source row inventory join",
    )
    replace_once(
        "services/api/tests/privacy/test_data_control.py",
        '''def test_retention_prunes_only_generated_exports(tmp_path: Path) -> None:\n    instants = iter([\n        datetime(2026, 1, 1, tzinfo=UTC),\n        datetime(2026, 1, 1, tzinfo=UTC),\n        datetime(2026, 3, 1, tzinfo=UTC),\n        datetime(2026, 3, 1, tzinfo=UTC),\n    ])\n    store, _engine, control = seeded(tmp_path, clock=lambda: next(instants))\n    artifact = control.create_archive("ws_koru_studio")\n    source_count = len(store.fetch_all("SELECT * FROM source_rows"))\n    control.configure_retention(\n        "ws_koru_studio", generated_export_days=30, auto_prune_exports=True\n    )\n    result = control.apply_retention("ws_koru_studio")\n''',
        '''def test_retention_prunes_only_generated_exports(tmp_path: Path) -> None:\n    now = [datetime(2026, 1, 1, tzinfo=UTC)]\n    store, _engine, control = seeded(tmp_path, clock=lambda: now[0])\n    artifact = control.create_archive("ws_koru_studio")\n    source_count = len(store.fetch_all("SELECT * FROM source_rows"))\n    now[0] = datetime(2026, 3, 1, tzinfo=UTC)\n    control.configure_retention(\n        "ws_koru_studio", generated_export_days=30, auto_prune_exports=True\n    )\n    result = control.apply_retention("ws_koru_studio")\n''',
        "retention test clock",
    )
    replace_once(
        "services/api/tests/privacy/test_data_control.py",
        '''    custom = CSV.read_bytes().replace(b"Acme", b"Owner")\n    await services.ingest_csv(\n        workspace_id="ws_koru_studio", filename="owner.csv", content=custom\n    )\n''',
        '''    custom = (\n        b"Date,Description,Amount,Reference\\n"\n        b"27/08/2026,Owner purchase,-10.00,owner-import-1\\n"\n    )\n    await services.ingest_csv(\n        workspace_id="ws_koru_studio", filename="owner.csv", content=custom\n    )\n''',
        "owner import fixture",
    )


if __name__ == "__main__":
    main()
