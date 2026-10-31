"""PostgreSQL migration DDL contracts without a live database or credentials."""
from types import SimpleNamespace

from sqlalchemy.dialects.postgresql import dialect


def test_legacy_postgresql_constraint_indexes_are_preserved_and_names_released(monkeypatch):
    from src.storage import legacy_migration

    indexes = [
        {"name": "antigravity_credentials_filename_key", "unique": True,
         "duplicates_constraint": "antigravity_credentials_filename_key"},
        {"name": "idx_ag_disabled", "unique": False},
        {"name": 'synthetic "quoted" index', "unique": False},
    ]
    inspector = SimpleNamespace(
        has_table=lambda name: True,
        get_columns=lambda name: [{"name": "filename"}],
        get_indexes=lambda name: indexes,
    )
    statements = []

    def execute(statement):
        sql = str(statement)
        # PostgreSQL's default RESTRICT behavior rejects this DDL because a
        # UNIQUE constraint depends on its backing index. The old code used it.
        assert not sql.startswith("DROP INDEX"), "legacy indexes must survive"
        statements.append(sql)

    connection = SimpleNamespace(dialect=dialect(), execute=execute)
    monkeypatch.setattr(legacy_migration, "inspect", lambda conn: inspector)
    monkeypatch.setattr(legacy_migration.time, "time_ns", lambda: 123456789)

    assert legacy_migration.rename_legacy_table(connection) == "antigravity_credentials_legacy_123456789"
    assert statements[0] == (
        "ALTER TABLE antigravity_credentials RENAME TO antigravity_credentials_legacy_123456789"
    )
    renames = statements[1:]
    assert len(renames) == len(indexes)
    targets = []
    for original, statement in zip(indexes, renames):
        quoted = connection.dialect.identifier_preparer.quote(original["name"])
        assert statement.startswith(f"ALTER INDEX {quoted} RENAME TO ")
        targets.append(statement.rsplit(" ", 1)[-1])
    assert len(set(targets)) == len(indexes)
    assert all(len(name) <= 63 and name.startswith("agy_legacy_") for name in targets)

    # A later migration in the same schema must not reuse the preserved names.
    monkeypatch.setattr(legacy_migration.time, "time_ns", lambda: 987654321)
    statements.clear()
    legacy_migration.rename_legacy_table(connection)
    assert set(targets).isdisjoint(sql.rsplit(" ", 1)[-1] for sql in statements[1:])
