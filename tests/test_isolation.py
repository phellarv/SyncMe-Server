import tempfile
from pathlib import Path

from app import db
from app.config import settings


def test_storage_settings_are_neutralised_for_every_test(tmp_path_factory):
    # A developer's .env (or environment) can point these at a live storage or database; the
    # autouse fixture in conftest.py must override them before any test touches the disk.
    for name in ("STORAGE_DIR", "FILES_DIR", "ARCHIVE_DIR", "DB_PATH"):
        assert getattr(settings, name) is None, name
    base = Path(tmp_path_factory.getbasetemp()).resolve()
    assert settings.DATA_DIR.resolve().is_relative_to(base)
    assert base.is_relative_to(Path(tempfile.gettempdir()).resolve())


def test_the_database_lives_in_the_per_test_data_directory(tmp_path_factory):
    db.get_conn()                           # leaves a cached connection behind, on purpose
    assert db.get_db_path().is_relative_to(Path(tmp_path_factory.getbasetemp()).resolve())


def test_no_database_connection_is_carried_over_from_an_earlier_test():
    # relies on running right after the test above: its connection must have been dropped
    assert not hasattr(db._local, "conn")
