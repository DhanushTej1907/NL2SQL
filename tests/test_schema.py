from __future__ import annotations

import sqlite3

from nl2sql.schema import classify_type, fk_stem, load_database, singularize


def test_tables_and_keys_are_introspected(university):
    symbols = university.symbols
    assert len(symbols.tables) == 5
    for table in symbols.tables.values():
        assert table.primary_key, f"{table.name} should have a declared primary key"
        assert table.row_count > 0

    composite = [t for t in symbols.tables.values() if len(t.primary_key) > 1]
    assert composite, "the sample has a table with a composite primary key"


def test_foreign_keys_are_declared_and_clean(university):
    edges = university.symbols.edges()
    assert edges
    assert all(e.status == "declared" for e in edges)
    assert all(e.trusted for e in edges)


def test_nullable_and_not_null_are_distinguished(university):
    nullable = [c for c in university.symbols.all_columns() if c.nullable]
    not_null = [c for c in university.symbols.all_columns() if not c.nullable]
    assert nullable and not_null


def test_integer_primary_keys_are_rowid_aliases(university):
    table = university.symbols.table("department")
    pk = table.pk_columns()[0]
    assert pk.rowid_alias and pk.never_null()
    assert table.has_trusted_pk()


def test_text_columns_are_sampled(university):
    sampled = [c for c in university.symbols.all_columns() if c.samples]
    assert sampled
    assert all(len(c.samples) <= 50 for c in sampled)


def test_schema_hash_is_stable_and_data_independent(university, tmp_path):
    import shutil

    copy = tmp_path / "copy.db"
    shutil.copyfile(university.path, copy)
    conn = sqlite3.connect(str(copy))
    first_table = next(iter(university.symbols.tables))
    conn.execute(f'DELETE FROM "{first_table}"')
    conn.commit()
    conn.close()
    assert load_database(copy).symbols.schema_hash == university.symbols.schema_hash


def test_connection_is_read_only(university):
    conn = university.connect()
    try:
        table = next(iter(university.symbols.tables))
        try:
            conn.execute(f'DELETE FROM "{table}"')
        except sqlite3.OperationalError as exc:
            assert "readonly" in str(exc).lower()
        else:  # pragma: no cover - would mean the guard failed
            raise AssertionError("a read-only connection accepted a DELETE")
    finally:
        conn.close()


def test_declared_foreign_key_is_demoted_when_data_breaks_it(violated_fk_db):
    edges = violated_fk_db.symbols.edges()
    assert edges
    assert any(e.status == "declared_violated" for e in edges)
    assert all(not e.trusted for e in edges if e.status == "declared_violated")


def test_edges_are_inferred_when_none_are_declared(nokeys):
    edges = nokeys.symbols.edges()
    assert edges, "key-shaped column names should produce inferred edges"
    assert all(e.status == "inferred" for e in edges)
    assert all(not e.trusted for e in edges)


def test_no_declared_keys_in_the_nokeys_sample(nokeys):
    for table in nokeys.symbols.tables.values():
        assert not table.primary_key
        assert not table.has_trusted_pk()


def test_label_column_is_chosen_by_shape(university):
    table = university.symbols.table("department")
    label = table.label_column()
    assert label is not None
    assert label.type_class == "text"
    assert not label.is_pk


def test_type_classification():
    assert classify_type("INTEGER") == "numeric"
    assert classify_type("REAL") == "numeric"
    assert classify_type("VARCHAR(40)") == "text"
    assert classify_type("DATE") == "date"
    assert classify_type("BLOB") == "other"


def test_key_stem_extraction():
    assert fk_stem("zorb_id") == "zorb"
    assert fk_stem("zorbId") == "zorb"
    assert fk_stem("weight") is None


def test_singularize():
    assert singularize("widgets") == "widget"
    assert singularize("entries") == "entry"
    assert singularize("boxes") == "box"


def test_compact_description_mentions_every_table(university):
    text = university.symbols.compact_description()
    for name in university.symbols.tables:
        assert name in text
