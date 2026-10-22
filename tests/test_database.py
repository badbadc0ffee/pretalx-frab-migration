import sqlite3
from contextlib import closing
from unittest.mock import Mock

import psycopg
import pymysql
import pytest

from pretalx_frab_migration import database
from pretalx_frab_migration.database import FrabDatabaseError, fetch_conferences


def test_sqlite_reads_conferences_without_changing_the_file(frab_database):
    original = frab_database.read_bytes()
    conferences = fetch_conferences(backend="sqlite3", name=str(frab_database))

    assert conferences == [(42, "Alpha & Friends", "AF"), (7, "Zeta Conference", "ZC")]
    assert frab_database.read_bytes() == original


def test_sqlite_connection_is_read_only_and_closed(frab_database, monkeypatch):
    connect = database._connect
    connections = []

    def capture_connection(**kwargs):
        connection = connect(**kwargs)
        connections.append(connection)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM conferences")
        return connection

    monkeypatch.setattr(database, "_connect", capture_connection)
    fetch_conferences(backend="sqlite3", name=str(frab_database))

    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")


def test_sqlite_missing_file_is_not_created(tmp_path):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(FrabDatabaseError, match="Could not connect"):
        fetch_conferences(backend="sqlite3", name=str(path))
    assert not path.exists()


def test_sqlite_requires_an_absolute_path():
    with pytest.raises(FrabDatabaseError, match="full SQLite file path"):
        fetch_conferences(backend="sqlite3", name="relative.sqlite3")


@pytest.mark.parametrize(
    ("backend", "name"), (("", ""), ("oracle", "frab"), ("sqlite3", ""))
)
def test_database_configuration_is_required(backend, name):
    with pytest.raises(FrabDatabaseError, match="Select a database type"):
        fetch_conferences(backend=backend, name=name)


@pytest.mark.parametrize("backend", ("mysql", "postgresql"))
@pytest.mark.parametrize("location", ("defaults", "host", "socket"))
def test_server_driver_options_and_connection_cleanup(monkeypatch, backend, location):
    driver = pymysql if backend == "mysql" else psycopg
    connection = Mock()
    cursor = connection.cursor.return_value
    cursor.fetchall.return_value = [(42, "Conference", "CONF")]
    connect = Mock(return_value=connection)
    monkeypatch.setattr(driver, "connect", connect)
    options = {"backend": backend, "name": "frab", "password": " secret "}
    if location != "defaults":
        options.update(host="db.example.org", port=6543, user="frab_reader")
    if location == "socket":
        options["host"] = (
            "/run/mysqld/mysqld.sock" if backend == "mysql" else "/run/postgresql"
        )

    assert fetch_conferences(**options) == [(42, "Conference", "CONF")]
    kwargs = connect.call_args.kwargs
    assert kwargs["database" if backend == "mysql" else "dbname"] == "frab"
    assert kwargs["password"] == " secret "
    assert kwargs["connect_timeout"] == 5
    if location == "defaults":
        assert kwargs["port"] == (3306 if backend == "mysql" else 5432)
        assert "host" not in kwargs
        assert "user" not in kwargs
    else:
        assert kwargs["port"] == 6543
        assert kwargs["user"] == "frab_reader"
        host_key = (
            "unix_socket" if location == "socket" and backend == "mysql" else "host"
        )
        assert kwargs[host_key] == options["host"]
    if backend == "mysql":
        assert kwargs["charset"] == "utf8mb4"
        assert kwargs["read_timeout"] == 10
        assert kwargs["write_timeout"] == 10
        assert cursor.execute.call_args_list[0].args == ("START TRANSACTION READ ONLY",)
    else:
        assert "default_transaction_read_only=on" in kwargs["options"]
        assert "statement_timeout=10000" in kwargs["options"]
    cursor.close.assert_called_once()
    connection.close.assert_called_once()


@pytest.mark.parametrize(
    ("backend", "driver", "error"),
    (
        ("mysql", pymysql, pymysql.OperationalError(1045, "password-secret")),
        ("postgresql", psycopg, psycopg.OperationalError("password-secret")),
    ),
)
def test_connection_errors_do_not_expose_credentials(
    monkeypatch, backend, driver, error
):
    monkeypatch.setattr(driver, "connect", Mock(side_effect=error))

    with pytest.raises(FrabDatabaseError, match="Could not connect") as exc:
        fetch_conferences(backend=backend, name="frab", password="password-secret")
    assert "password-secret" not in str(exc.value)


@pytest.mark.parametrize("backend", ("mysql", "postgresql"))
def test_query_errors_close_the_connection(monkeypatch, backend):
    driver = pymysql if backend == "mysql" else psycopg
    connection = Mock()
    cursor = connection.cursor.return_value
    cursor.execute.side_effect = driver.ProgrammingError("private query details")
    monkeypatch.setattr(driver, "connect", Mock(return_value=connection))

    with pytest.raises(
        FrabDatabaseError, match="conference list could not be read"
    ) as exc:
        fetch_conferences(backend=backend, name="frab")
    assert "private query details" not in str(exc.value)
    cursor.close.assert_called_once()
    connection.close.assert_called_once()


def test_selected_conference_resolves_parent_values(migration_database):
    with closing(sqlite3.connect(migration_database)) as connection:
        connection.execute("""
            INSERT INTO conferences (id, title, acronym, timezone, start_date, end_date, parent_id)
            VALUES (7, 'Child', 'CHILD', 'UTC', '2020-01-01', '2020-01-01', 42)
        """)
        connection.commit()
    original = migration_database.read_bytes()
    row = database.fetch_conference(7, backend="sqlite3", name=str(migration_database))
    assert row["id"] == 7
    assert row["title"] == "Child"
    assert row["timezone"] == "Berlin"
    assert row["start_date"] == "2026-10-21 23:30:00"
    assert row["end_date"] == "2026-10-24 21:00:00"
    assert "feedback_token" not in row
    assert migration_database.read_bytes() == original


@pytest.mark.parametrize(
    ("parent_id", "message"), ((42, "parent cycle"), (999, "no longer exists"))
)
def test_invalid_parent_stops_the_read(migration_database, parent_id, message):
    with closing(sqlite3.connect(migration_database)) as connection:
        connection.execute(
            "UPDATE conferences SET parent_id = ? WHERE id = 42", (parent_id,)
        )
        connection.commit()
    with pytest.raises(FrabDatabaseError, match=message):
        database.fetch_conference(42, backend="sqlite3", name=str(migration_database))


@pytest.mark.parametrize("backend", ("mysql", "postgresql"))
def test_server_selected_conference_uses_bound_parameters(monkeypatch, backend):
    driver = pymysql if backend == "mysql" else psycopg
    connection = Mock()
    cursor = connection.cursor.return_value
    cursor.fetchone.return_value = (
        42,
        "Title",
        "CONF",
        "UTC",
        None,
        None,
        "orga@example.org",
        None,
        0,
        None,
        None,
        None,
    )
    monkeypatch.setattr(driver, "connect", Mock(return_value=connection))
    row = database.fetch_conference(42, backend=backend, name="frab")
    query, parameters = cursor.execute.call_args.args
    assert query.endswith("WHERE id = %s")
    assert parameters == (42,)
    assert "feedback_token" not in query
    assert "schedule_public" not in query
    assert row["id"] == 42
    cursor.close.assert_called_once()
    connection.close.assert_called_once()


def test_selected_conference_does_not_interpolate_sql(migration_database):
    with pytest.raises(FrabDatabaseError, match="no longer exists"):
        database.fetch_conference(
            "42 OR 1=1", backend="sqlite3", name=str(migration_database)
        )


@pytest.mark.parametrize("backend", ("mysql", "postgresql"))
def test_server_cfp_email_lookup_uses_bound_parameters(monkeypatch, backend):
    driver = pymysql if backend == "mysql" else psycopg
    connection = Mock()
    cursor = connection.cursor.return_value
    cursor.fetchone.side_effect = [
        (42, "Title", "CONF", "UTC", None, None, None, None, 0, None, None, None),
        ("cfp@example.org",),
    ]
    monkeypatch.setattr(driver, "connect", Mock(return_value=connection))

    row = database.fetch_conference(42, backend=backend, name="frab")

    assert row["email"] == "cfp@example.org"
    query, parameters = cursor.execute.call_args.args
    assert query == (
        "SELECT contact_email FROM call_for_participations "
        "WHERE conference_id = %s ORDER BY id LIMIT 1"
    )
    assert parameters == (42,)
    cursor.close.assert_called_once()
    connection.close.assert_called_once()


def test_child_conference_does_not_use_its_parents_cfp_email(migration_database):
    with closing(sqlite3.connect(migration_database)) as connection:
        connection.execute("""
            INSERT INTO conferences (id, title, acronym, parent_id)
            VALUES (7, 'Child', 'CHILD', 42)
        """)
        connection.execute("""
            INSERT INTO call_for_participations (conference_id, contact_email)
            VALUES (42, 'parent-cfp@example.org')
        """)
        connection.commit()

    row = database.fetch_conference(7, backend="sqlite3", name=str(migration_database))

    assert row["email"] is None
