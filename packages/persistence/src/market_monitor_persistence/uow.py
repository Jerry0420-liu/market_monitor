from collections.abc import Mapping
from typing import Any

from sqlalchemy import Column, Table, update
from sqlalchemy.engine import Connection
from sqlalchemy.sql.elements import ColumnElement


def optimistic_update(
    connection: Connection,
    table: Table,
    identity: ColumnElement[bool],
    version_column: Column[int],
    expected_version: int,
    values: Mapping[str, Any],
) -> bool:
    updated_values = dict(values)
    updated_values[version_column.key] = expected_version + 1
    result = connection.execute(
        update(table)
        .where(identity)
        .where(version_column == expected_version)
        .values(**updated_values)
    )
    return result.rowcount == 1
