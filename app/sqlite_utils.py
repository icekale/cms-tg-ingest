from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def sqlite_connection(
    database,
    *,
    uri=False,
    read_only=False,
    timeout=30,
    row_factory=None,
) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(database, uri=uri, timeout=timeout)
    if row_factory is not None:
        connection.row_factory = row_factory
    try:
        yield connection
        if not read_only:
            connection.commit()
    except Exception:
        if not read_only:
            connection.rollback()
        raise
    finally:
        connection.close()


def sqlite_quick_check(database: str | Path) -> None:
    path = Path(database).expanduser().resolve()
    with sqlite_connection(f"{path.as_uri()}?mode=ro", uri=True, read_only=True) as connection:
        row = connection.execute("PRAGMA quick_check").fetchone()
    diagnostic = row[0] if row else ""
    if diagnostic != "ok":
        raise sqlite3.DatabaseError(str(diagnostic))
