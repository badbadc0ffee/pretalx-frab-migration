import sqlite3
from contextlib import closing
from unittest.mock import Mock

import pymysql
import pytest
from django.contrib import messages
from django.test import Client
from django.urls import reverse

from pretalx.common.models.settings import GlobalSettings

from pretalx_frab_migration import views

pytestmark = pytest.mark.django_db


@pytest.fixture
def connection_data(frab_database):
    return {
        "action": "connect",
        "frab_migration_db_backend": "sqlite3",
        "frab_migration_db_name": str(frab_database),
    }


def test_page_and_save_do_not_connect(
    admin_client, settings_url, connection_data, monkeypatch
):
    fetch = Mock()
    monkeypatch.setattr(views, "fetch_conferences", fetch)

    response = admin_client.get(settings_url)
    assert response.status_code == 200
    assert b'value="connect"' in response.content
    assert b"<h3>Conferences</h3>" not in response.content
    assert b'name="conference"' not in response.content
    data = {**connection_data, "action": "save"}
    response = admin_client.post(settings_url, data)
    assert response.status_code == 302
    fetch.assert_not_called()


def test_connect_shows_conferences_in_organiser_context(
    admin_client, organiser, settings_url, connection_data
):
    response = admin_client.post(settings_url, connection_data)

    assert response.status_code == 200
    assert response.context["connected"] is True
    assert b"<h3>Conferences</h3>" in response.content
    notices = list(response.context["messages"])
    assert [(notice.level, str(notice)) for notice in notices] == [
        (messages.SUCCESS, "Connection successful. Select a conference.")
    ]
    assert response.content.count(b"Connection successful. Select a conference.") == 1
    assert response.wsgi_request.organiser == organiser
    assert response.wsgi_request.event is None
    choices = response.context["conference_form"].fields["conference"].choices
    assert choices[1:] == [
        ("42", "Alpha & Friends (AF)"),
        ("7", "Zeta Conference (ZC)"),
    ]
    assert b"Alpha &amp; Friends (AF)" in response.content
    assert b"<select" in response.content
    assert (
        GlobalSettings().settings.get("frab_migration_db_name")
        == connection_data["frab_migration_db_name"]
    )
    assert list(organiser.events.all()) == []
    html = response.content.decode()
    assert (
        html.index('name="frab_migration_db_backend"')
        < html.index('value="connect"')
        < html.index('name="conference"')
    )


def test_connect_uses_the_saved_password(admin_client, settings_url, monkeypatch):
    GlobalSettings().settings.set("frab_migration_db_password", "saved-password-secret")
    fetch = Mock(return_value=[(1, "Conference", "CONF")])
    monkeypatch.setattr(views, "fetch_conferences", fetch)
    response = admin_client.post(
        settings_url,
        {
            "action": "connect",
            "frab_migration_db_backend": "postgresql",
            "frab_migration_db_name": "frab",
            "frab_migration_db_password": "",
        },
    )

    assert response.status_code == 200
    assert fetch.call_args.kwargs["password"] == "saved-password-secret"
    assert b"saved-password-secret" not in response.content


def test_connect_failure_keeps_saved_settings_and_hides_password(
    admin_client, settings_url, monkeypatch
):
    GlobalSettings().settings.set("frab_migration_db_name", "saved-frab")
    connect = Mock(side_effect=pymysql.OperationalError(1045, "new-password-secret"))
    monkeypatch.setattr(pymysql, "connect", connect)
    response = admin_client.post(
        settings_url,
        {
            "action": "connect",
            "frab_migration_db_backend": "mysql",
            "frab_migration_db_name": "new-frab",
            "frab_migration_db_password": "new-password-secret",
        },
    )

    assert response.status_code == 200
    assert "Could not connect" in str(list(response.context["messages"])[0])
    assert list(response.context["messages"])[0].level == messages.ERROR
    assert b"<h3>Conferences</h3>" not in response.content
    assert b"new-password-secret" not in response.content
    assert b'name="conference"' not in response.content
    assert GlobalSettings().settings.get("frab_migration_db_name") == "saved-frab"


def test_empty_database_shows_a_message(
    admin_client, settings_url, connection_data, frab_database
):
    with closing(sqlite3.connect(frab_database)) as connection:
        connection.execute("DELETE FROM conferences")
        connection.commit()
    response = admin_client.post(settings_url, connection_data)

    assert response.context["connected"] is True
    assert b"<h3>Conferences</h3>" in response.content
    assert response.context["conference_form"] is None
    assert [(notice.level, str(notice)) for notice in response.context["messages"]] == [
        (messages.SUCCESS, "Connection successful. This database has no conferences.")
    ]


def test_wrong_database_schema_shows_an_error(
    admin_client, settings_url, connection_data, frab_database
):
    with closing(sqlite3.connect(frab_database)) as connection:
        connection.execute("DROP TABLE conferences")
        connection.commit()
    response = admin_client.post(settings_url, connection_data)

    assert response.status_code == 200
    assert "conference list could not be read" in str(
        list(response.context["messages"])[0]
    )
    assert b'name="conference"' not in response.content
    assert GlobalSettings().settings.get("frab_migration_db_backend") is None


def test_connect_requires_database_settings(admin_client, settings_url):
    response = admin_client.post(settings_url, {"action": "connect"})

    assert response.status_code == 200
    assert "Select a database type" in str(list(response.context["messages"])[0])


def test_invalid_settings_do_not_connect(
    admin_client, settings_url, connection_data, monkeypatch
):
    fetch = Mock()
    monkeypatch.setattr(views, "fetch_conferences", fetch)
    response = admin_client.post(
        settings_url, {**connection_data, "frab_migration_db_name": ""}
    )

    assert response.status_code == 200
    assert "frab_migration_db_name" in response.context["form"].errors
    fetch.assert_not_called()


@pytest.mark.parametrize("client_fixture", ("orga_client", "review_client", "client"))
def test_only_administrators_can_connect(
    request, client_fixture, settings_url, connection_data, monkeypatch
):
    client = request.getfixturevalue(client_fixture)
    fetch = Mock()
    monkeypatch.setattr(views, "fetch_conferences", fetch)
    response = client.post(settings_url, connection_data)

    assert response.status_code == (302 if client_fixture == "client" else 404)
    fetch.assert_not_called()


def test_connect_requires_csrf(admin_user, settings_url, connection_data, monkeypatch):
    client = Client(enforce_csrf_checks=True)
    client.force_login(admin_user)
    fetch = Mock()
    monkeypatch.setattr(views, "fetch_conferences", fetch)
    response = client.post(settings_url, connection_data)

    assert response.status_code == 403
    fetch.assert_not_called()


def test_conference_list_is_not_kept_for_another_request(
    admin_client, organiser, settings_url, connection_data
):
    response = admin_client.post(settings_url, connection_data)
    assert response.context["connected"] is True
    assert b"<h3>Conferences</h3>" in response.content
    response = admin_client.get(settings_url)
    assert not list(response.context["messages"])
    assert b"<h3>Conferences</h3>" not in response.content
    assert b'name="conference"' not in response.content
    response = admin_client.post(
        reverse(
            "plugins:pretalx_frab_migration:settings", kwargs={"organiser": "missing"}
        ),
        connection_data,
    )
    assert response.status_code == 404
