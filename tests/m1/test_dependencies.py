from importlib.metadata import version


def test_persistence_dependencies_are_compatibility_pinned() -> None:
    assert version("SQLAlchemy") == "2.0.51"
    assert version("alembic") == "1.18.5"
