from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def patch_store() -> None:
    path = ROOT / "services/api/src/finance_agent/storage/store.py"
    content = path.read_text()
    content = content.replace(
        '''            connection.close()
            connection = None
            if database_path is not None:
''',
        '''            connection.close()
            if database_path is not None:
''',
        1,
    )
    content = content.replace(
        '''        except Exception as exc:
            if connection is not None:
                connection.close()
''',
        '''        except Exception as exc:
            connection.close()
''',
        1,
    )
    content = content.replace(
        '''        finally:
            if connection is not None:
                connection.close()
''',
        '''        finally:
            connection.close()
''',
        1,
    )
    path.write_text(content)


def patch_test() -> None:
    path = ROOT / "services/api/tests/storage/test_migration_safety.py"
    content = path.read_text()
    old = '''    original = database.read_bytes()
    version = max(migration.version for migration in store_module.MIGRATIONS) + 1
'''
    new = '''    import sqlite3

    checkpoint = sqlite3.connect(database)
    try:
        checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        checkpoint.close()
    original = database.read_bytes()
    version = max(migration.version for migration in store_module.MIGRATIONS) + 1
'''
    if old not in content:
        raise RuntimeError("migration restoration test marker missing")
    path.write_text(content.replace(old, new, 1))


if __name__ == "__main__":
    patch_store()
    patch_test()
    print("migration safety verification correction applied")
