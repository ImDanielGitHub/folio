from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "apply_audit_programme_v4.py"
spec = importlib.util.spec_from_file_location("audit_programme_v4", SOURCE)
if spec is None or spec.loader is None:
    raise RuntimeError("could not load audit programme v4")
programme_v4 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(programme_v4)

# SQLite's stdlib typing exposes row subscripts as Any. Convert values at the
# persistence boundary so strict MyPy checks downstream domain code soundly.
path = ROOT / "services/api/src/finance_agent/storage/store.py"
content = path.read_text(encoding="utf-8")
content = content.replace(
    "from typing import Any\n",
    "from typing import Any, cast\n",
    1,
)
content = content.replace(
    "            return connection.execute(sql, parameters).fetchone()\n",
    "            return cast(sqlite3.Row | None, connection.execute(sql, parameters).fetchone())\n",
    1,
)
path.write_text(content, encoding="utf-8")

path = ROOT / "services/api/src/finance_agent/agent/business_discovery.py"
content = path.read_text(encoding="utf-8")
content = content.replace(
    "from dataclasses import dataclass\n",
    "from dataclasses import dataclass\nfrom datetime import datetime\n",
    1,
)
content = content.replace(
    "    asked_at,\n",
    "    asked_at: datetime,\n",
    1,
)
path.write_text(content, encoding="utf-8")

path = ROOT / "services/api/src/finance_agent/finance/service.py"
content = path.read_text(encoding="utf-8")
content = content.replace(
    "from typing import Any\n",
    "from typing import Any, cast\n",
    1,
)
content = content.replace(
    '''        invalid = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM transactions
            WHERE workspace_id = ?
              AND (currency != 'NZD' OR typeof(amount_minor) != 'integer')
            """,
            (WORKSPACE_ID,),
        ).fetchone()["count"]
''',
    '''        invalid = int(
            connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM transactions
                WHERE workspace_id = ?
                  AND (currency != 'NZD' OR typeof(amount_minor) != 'integer')
                """,
                (WORKSPACE_ID,),
            ).fetchone()["count"]
        )
''',
    1,
)
content = content.replace(
    '''        return connection.execute(
            "SELECT COUNT(*) AS count FROM transactions WHERE workspace_id = ?",
            (WORKSPACE_ID,),
        ).fetchone()["count"]
''',
    '''        return int(
            connection.execute(
                "SELECT COUNT(*) AS count FROM transactions WHERE workspace_id = ?",
                (WORKSPACE_ID,),
            ).fetchone()["count"]
        )
''',
    1,
)
content = content.replace(
    '''        return connection.execute(
            "SELECT state_revision FROM workspaces WHERE workspace_id = ?",
            (WORKSPACE_ID,),
        ).fetchone()["state_revision"]
''',
    '''        return int(
            connection.execute(
                "SELECT state_revision FROM workspaces WHERE workspace_id = ?",
                (WORKSPACE_ID,),
            ).fetchone()["state_revision"]
        )
''',
    1,
)
content = content.replace(
    ''').fetchone()["revision"]
        connection.execute(
''',
    ''').fetchone()["revision"]
        revision = int(revision)
        connection.execute(
''',
    1,
)
content = content.replace(
    '        return _json(row["snapshot_json"])\n',
    '        return cast(dict[str, Any], _json(row["snapshot_json"]))\n',
    1,
)
path.write_text(content, encoding="utf-8")

print("Audit programme v5 strict typing fixes applied")
