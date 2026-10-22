import sqlite3
from contextlib import closing
from pathlib import Path

import psycopg
import pymysql
from django.utils.translation import gettext_lazy as _

CONNECT_TIMEOUT = 5
READ_TIMEOUT = 10
DATABASE_ERRORS = (
    sqlite3.Error,
    pymysql.MySQLError,
    psycopg.Error,
    OSError,
    ValueError,
)


class FrabDatabaseError(Exception):
    pass


def _connect(*, backend, name, host, port, user, password):
    if backend == "sqlite3":
        path = Path(name)
        if not path.is_absolute():
            raise FrabDatabaseError(
                _("Enter the full SQLite file path on the pretalx server.")
            )
        return sqlite3.connect(
            f"{path.as_uri()}?mode=ro", uri=True, timeout=CONNECT_TIMEOUT
        )
    if backend == "mysql":
        options = {
            "database": name,
            "port": port or 3306,
            "password": password,
            "charset": "utf8mb4",
            "connect_timeout": CONNECT_TIMEOUT,
            "read_timeout": READ_TIMEOUT,
            "write_timeout": READ_TIMEOUT,
        }
        if host.startswith("/"):
            options["unix_socket"] = host
        elif host:
            options["host"] = host
        if user:
            options["user"] = user
        return pymysql.connect(**options)
    options = {
        "dbname": name,
        "port": port or 5432,
        "password": password,
        "connect_timeout": CONNECT_TIMEOUT,
        "options": f"-c default_transaction_read_only=on -c statement_timeout={READ_TIMEOUT * 1000}",
    }
    if host:
        options["host"] = host
    if user:
        options["user"] = user
    return psycopg.connect(**options)


def fetch_conferences(*, backend, name, host="", port=None, user="", password=""):
    if backend not in ("sqlite3", "mysql", "postgresql") or not name:
        raise FrabDatabaseError(
            _("Select a database type and enter the database name or file path.")
        )
    try:
        connection = _connect(
            backend=backend,
            name=name,
            host=host,
            port=port,
            user=user,
            password=password,
        )
    except DATABASE_ERRORS:
        raise FrabDatabaseError(
            _(
                "Could not connect to the database. Check the access settings and try again."
            )
        ) from None

    try:
        with closing(connection), closing(connection.cursor()) as cursor:
            if backend == "mysql":
                cursor.execute("START TRANSACTION READ ONLY")
            cursor.execute(
                "SELECT id, title, acronym FROM conferences ORDER BY title, id"
            )
            return cursor.fetchall()
    except DATABASE_ERRORS:
        raise FrabDatabaseError(
            _(
                "The database is reachable, but the conference list could not be read. "
                "Check that this is a frab database and that the database user can read its conferences."
            )
        ) from None
