import pytest

from pretalx.common.models.settings import GlobalSettings

DB_PREFIX = "frab_migration_db_"

pytestmark = pytest.mark.django_db


def database_data(**values):
    return {f"{DB_PREFIX}{key}": value for key, value in values.items()}


@pytest.fixture
def saved_database():
    data = database_data(
        backend="mysql",
        name="frab",
        host="db.example.org",
        port=3307,
        user="frab_reader",
        password="saved-database-secret",
    )
    for key, value in data.items():
        GlobalSettings().settings.set(key, value)
    return data


@pytest.mark.parametrize(
    ("backend", "port"),
    (("mysql", "3306"), ("postgresql", "5432"), ("postgresql", "6543")),
)
def test_save_server_database_settings(admin_client, backend, port, settings_url):
    data = database_data(
        backend=backend,
        name="frab",
        host="db.example.org",
        port=port,
        user="frab_reader",
        password="  secret with spaces  ",
    )
    url = settings_url
    response = admin_client.post(url, data)

    assert response.status_code == 302
    assert response.url == url
    settings = GlobalSettings().settings
    for key, value in data.items():
        assert settings.get(key, as_type=str) == value

    response = admin_client.get(url)
    form = response.context["form"]
    for field in ("backend", "name", "host", "port", "user"):
        assert str(form[f"{DB_PREFIX}{field}"].value()) == data[f"{DB_PREFIX}{field}"]
    assert form[f"{DB_PREFIX}password"].value() in (None, "")
    assert b"secret with spaces" not in response.content


@pytest.mark.parametrize("backend", ("mysql", "postgresql"))
def test_server_database_can_use_local_defaults(admin_client, backend, settings_url):
    response = admin_client.post(
        settings_url, database_data(backend=backend, name="frab")
    )

    assert response.status_code == 302
    settings = GlobalSettings().settings
    assert settings.get(f"{DB_PREFIX}backend") == backend
    assert settings.get(f"{DB_PREFIX}port") is None
    for field in ("host", "user", "password"):
        assert settings.get(f"{DB_PREFIX}{field}") == ""


@pytest.mark.parametrize(
    ("backend", "host"),
    (("mysql", "/run/mysqld/mysqld.sock"), ("postgresql", "/run/postgresql")),
)
def test_server_database_accepts_socket_paths(
    admin_client, backend, host, settings_url
):
    response = admin_client.post(
        settings_url, database_data(backend=backend, name="frab", host=host)
    )

    assert response.status_code == 302
    assert GlobalSettings().settings.get(f"{DB_PREFIX}host") == host


@pytest.mark.parametrize(
    "file_path", ("/srv/frab/db/production.sqlite3", "C:/frab/db/production.sqlite3")
)
def test_save_sqlite_path(admin_client, file_path, settings_url):
    response = admin_client.post(
        settings_url, database_data(backend="sqlite3", name=file_path)
    )

    assert response.status_code == 302
    assert GlobalSettings().settings.get(f"{DB_PREFIX}backend") == "sqlite3"
    assert GlobalSettings().settings.get(f"{DB_PREFIX}name") == file_path


def test_saving_sqlite_settings_does_not_create_database(
    admin_client, tmp_path, settings_url
):
    file_path = tmp_path / "frab.sqlite3"
    response = admin_client.post(
        settings_url, database_data(backend="sqlite3", name=str(file_path))
    )

    assert response.status_code == 302
    assert not file_path.exists()


@pytest.mark.parametrize("backend", ("sqlite3", "mysql", "postgresql"))
def test_database_name_is_required(admin_client, backend, settings_url):
    response = admin_client.post(settings_url, database_data(backend=backend, name=" "))

    assert response.status_code == 200
    assert f"{DB_PREFIX}name" in response.context["form"].errors
    assert GlobalSettings().settings.get(f"{DB_PREFIX}backend") is None


@pytest.mark.parametrize(
    ("field", "value"),
    (("backend", "oracle"), ("port", "0"), ("port", "65536"), ("port", "invalid")),
)
def test_invalid_database_settings_keep_saved_values(
    admin_client, saved_database, field, value, settings_url
):
    data = {
        **saved_database,
        f"{DB_PREFIX}{field}": value,
        f"{DB_PREFIX}password": "new-database-secret",
    }
    response = admin_client.post(settings_url, data)

    assert response.status_code == 200
    assert f"{DB_PREFIX}{field}" in response.context["form"].errors
    assert b"saved-database-secret" not in response.content
    assert b"new-database-secret" not in response.content
    for key, saved_value in saved_database.items():
        assert (
            GlobalSettings().settings.get(key, as_type=type(saved_value)) == saved_value
        )


@pytest.mark.parametrize("password", ("", " new-database-secret "))
def test_password_is_kept_or_replaced(
    admin_client, saved_database, password, settings_url
):
    data = {**saved_database, f"{DB_PREFIX}password": password}
    response = admin_client.post(settings_url, data)

    assert response.status_code == 302
    assert GlobalSettings().settings.get(f"{DB_PREFIX}password") == (
        password or "saved-database-secret"
    )


def test_sqlite_clears_server_settings(admin_client, saved_database, settings_url):
    data = {
        **saved_database,
        **database_data(backend="sqlite3", name="/srv/frab/db/production.sqlite3"),
    }
    response = admin_client.post(settings_url, data)

    assert response.status_code == 302
    settings = GlobalSettings().settings
    assert settings.get(f"{DB_PREFIX}backend") == "sqlite3"
    assert settings.get(f"{DB_PREFIX}name") == "/srv/frab/db/production.sqlite3"
    for field in ("host", "user", "password"):
        assert settings.get(f"{DB_PREFIX}{field}") == ""
    assert settings.get(f"{DB_PREFIX}port") is None


def test_disable_database_clears_database_settings(
    admin_client, saved_database, settings_url
):
    data = {
        **saved_database,
        f"{DB_PREFIX}backend": "",
        "frab_migration_url": "https://frab.example.org/",
    }
    response = admin_client.post(settings_url, data)

    assert response.status_code == 302
    settings = GlobalSettings().settings
    for field in ("backend", "name", "host", "user", "password"):
        assert settings.get(f"{DB_PREFIX}{field}") == ""
    assert settings.get(f"{DB_PREFIX}port") is None
    assert settings.get("frab_migration_url") == "https://frab.example.org/"
