from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def patch_migration() -> None:
    path = ROOT / "services/api/src/finance_agent/storage/migrations.py"
    content = path.read_text()
    old = '''        CREATE TRIGGER fx_rates_no_update
        BEFORE UPDATE ON fx_rate_revisions
        BEGIN
            SELECT RAISE(ABORT, 'FX rate revisions are append-only');
        END;
'''
    new = '''        CREATE TRIGGER fx_rates_content_no_update
        BEFORE UPDATE OF rate_id, revision, workspace_id, base_currency,
            quote_currency, rate_numerator, rate_denominator, effective_on,
            source_label, evidence_id, created_at
        ON fx_rate_revisions
        BEGIN
            SELECT RAISE(ABORT, 'FX rate revision content is append-only');
        END;

        CREATE TRIGGER fx_rates_status_transition_only
        BEFORE UPDATE OF status ON fx_rate_revisions
        WHEN NOT (OLD.status = 'active' AND NEW.status = 'superseded')
        BEGIN
            SELECT RAISE(ABORT, 'invalid FX rate status transition');
        END;
'''
    if old not in content:
        raise RuntimeError("FX all-update trigger block not found")
    path.write_text(content.replace(old, new, 1))


def patch_tests() -> None:
    path = ROOT / "services/api/tests/finance/test_explicit_fx_conversion.py"
    content = path.read_text()
    addition = '''

def test_new_rate_revision_supersedes_prior_without_editing_content(tmp_path: Path) -> None:
    import sqlite3

    store, _staged, service = setup(tmp_path)
    first = service.add_rate(
        workspace_id="ws_koru_studio",
        base_currency="USD",
        effective_on="2026-08-01",
        rate_numerator=160,
        rate_denominator=100,
        source_label="First documented rate",
        evidence_id="evd_koru_bank_csv",
    )
    second = service.add_rate(
        workspace_id="ws_koru_studio",
        base_currency="USD",
        effective_on="2026-08-01",
        rate_numerator=162,
        rate_denominator=100,
        source_label="Corrected documented rate",
        evidence_id="evd_koru_bank_csv",
    )
    assert first["rateId"] == second["rateId"]
    assert second["revision"] == 2
    rows = store.fetch_all(
        "SELECT revision, rate_numerator, status FROM fx_rate_revisions WHERE rate_id = ? ORDER BY revision",
        (first["rateId"],),
    )
    assert [(int(row["revision"]), int(row["rate_numerator"]), str(row["status"])) for row in rows] == [
        (1, 160, "superseded"),
        (2, 162, "active"),
    ]
    try:
        with store.transaction() as connection:
            connection.execute(
                "UPDATE fx_rate_revisions SET rate_numerator = 999 WHERE rate_id = ? AND revision = 1",
                (first["rateId"],),
            )
    except sqlite3.IntegrityError as exc:
        assert "append-only" in str(exc)
    else:
        raise AssertionError("historical FX rate content was edited")
'''
    if "test_new_rate_revision_supersedes_prior_without_editing_content" not in content:
        path.write_text(content.rstrip() + addition + "\n")


if __name__ == "__main__":
    patch_migration()
    patch_tests()
    print("FX revision immutability correction applied")
