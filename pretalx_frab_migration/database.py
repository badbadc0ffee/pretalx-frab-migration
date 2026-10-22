import sqlite3
from contextlib import closing, contextmanager
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


@contextmanager
def _cursor(*, backend, name, host="", port=None, user="", password="", detail=False):
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
            yield cursor
    except DATABASE_ERRORS:
        raise FrabDatabaseError(
            _(
                "The database is reachable, but the selected conference could not be read. "
                "Check that this is a frab database and that the database user can read "
                "the conferences, call_for_participations, and tracks tables."
            )
            if detail
            else _(
                "The database is reachable, but the conference list could not be read. "
                "Check that this is a frab database and that the database user can read its conferences."
            )
        ) from None


def fetch_conferences(**options):
    with _cursor(**options) as cursor:
        cursor.execute("SELECT id, title, acronym FROM conferences ORDER BY title, id")
        return cursor.fetchall()


CONFERENCE_FIELDS = (
    "id",
    "title",
    "acronym",
    "timezone",
    "start_date",
    "end_date",
    "email",
    "color",
    "feedback_enabled",
    "schedule_html_intro",
    "parent_id",
    "program_export_base_url",
)


CFP_FIELDS = (
    "start_date",
    "end_date",
    "hard_deadline",
    "welcome_text",
    "contact_email",
)


def fetch_conference(conference_id, **options):
    """Read one conference, its inherited dates and timezone, its CfP, and its tracks."""
    placeholder = "?" if options["backend"] == "sqlite3" else "%s"
    # Only constant column names and the driver's placeholder enter the SQL text.
    query = f"SELECT {', '.join(CONFERENCE_FIELDS)} FROM conferences WHERE id = {placeholder}"  # noqa: S608
    with _cursor(**options, detail=True) as cursor:

        def read_row(row_id):
            cursor.execute(query, (row_id,))
            row = cursor.fetchone()
            if row is None:
                raise FrabDatabaseError(
                    _("The conference or its parent no longer exists. Connect again.")
                )
            return dict(zip(CONFERENCE_FIELDS, row, strict=True))

        conference = read_row(conference_id)
        parent = conference
        seen = {conference["id"]}
        while parent["parent_id"] is not None:
            if parent["parent_id"] in seen:
                raise FrabDatabaseError(
                    _("The conference has a parent cycle. Correct the source data.")
                )
            parent = read_row(parent["parent_id"])
            seen.add(parent["id"])
        for field in ("timezone", "start_date", "end_date"):
            conference[field] = parent[field]
        cfp_query = f"SELECT {', '.join(CFP_FIELDS)} FROM call_for_participations WHERE conference_id = {placeholder} ORDER BY id LIMIT 1"  # noqa: S608 -- constant columns and driver placeholder only
        cursor.execute(cfp_query, (conference["id"],))
        row = cursor.fetchone()
        conference["cfp"] = dict(zip(CFP_FIELDS, row, strict=True)) if row else None
        if conference["cfp"] and not (conference["email"] or "").strip():
            conference["email"] = (conference["cfp"]["contact_email"] or "").strip()
        track_query = f"SELECT id, name, color FROM tracks WHERE conference_id = {placeholder} ORDER BY name, id"  # noqa: S608 -- driver placeholder only
        cursor.execute(track_query, (conference["id"],))
        conference["tracks"] = [
            dict(zip(("id", "name", "color"), row, strict=True))
            for row in cursor.fetchall()
        ]
        return conference
