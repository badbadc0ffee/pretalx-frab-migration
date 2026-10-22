import datetime as dt
import sqlite3
from contextlib import closing
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from bs4 import BeautifulSoup
from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import Client
from django.urls import reverse
from django_scopes import scopes_disabled

from pretalx.common.models import ActivityLog
from pretalx.common.models.settings import GlobalSettings
from pretalx.event.domain import event as event_domain
from pretalx.event.models import Event, Organiser
from pretalx.event.models.event import default_display_settings, default_feature_flags
from pretalx.mail.models import QueuedMail

from pretalx_frab_migration import migration, views
from pretalx_frab_migration.database import fetch_conference
from pretalx_frab_migration.migration import (
    conference_boolean,
    conference_date,
    conference_intro,
    conference_timezone,
    migrate_conference,
)

pytestmark = pytest.mark.django_db


def update_source(path, **fields):
    with closing(sqlite3.connect(path)) as connection:
        assignments = ", ".join(f"{key} = ?" for key in fields)
        connection.execute(
            f"UPDATE conferences SET {assignments} WHERE id = 42",
            tuple(fields.values()),
        )
        connection.commit()


@pytest.fixture
def migration_post(admin_client, settings_url, migration_database):
    response = admin_client.post(
        settings_url,
        {
            "action": "connect",
            "frab_migration_db_backend": "sqlite3",
            "frab_migration_db_name": str(migration_database),
            "frab_migration_url": "https://frab.example.org",
        },
    )
    assert response.status_code == 200
    soup = BeautifulSoup(response.content, "html.parser")
    button = soup.find("button", value="migrate")
    assert button["type"] == "submit"
    assert not button.has_attr("disabled")
    assert button.find_parent("form").find("select", attrs={"name": "conference"})
    return {
        "action": "migrate",
        "conference": "42",
        "connection": response.context["conference_form"].initial["connection"],
    }


def test_migrate_creates_only_the_event_with_native_initialisation(
    admin_client,
    admin_user,
    organiser,
    settings_url,
    migration_database,
    migration_post,
    monkeypatch,
):
    original = migration_database.read_bytes()
    create = Mock(wraps=migration.create_event)
    post_create = Mock(wraps=migration.post_create_event)
    monkeypatch.setattr(migration, "create_event", create)
    monkeypatch.setattr(migration, "post_create_event", post_create)
    response = admin_client.post(settings_url, migration_post)

    assert response.status_code == 302
    with scopes_disabled():
        event = Event.objects.get()
        assert event.organiser_id == organiser.pk
        assert event.slug == "af-2026"
        assert event.name.localize(settings.LANGUAGE_CODE) == "Alpha & Friends"
        assert event.locale == settings.LANGUAGE_CODE
        assert event.locales == event.content_locales == [settings.LANGUAGE_CODE]
        assert event.timezone == "Europe/Berlin"
        assert event.date_from == dt.date(2026, 10, 22)
        assert event.date_to == dt.date(2026, 10, 24)
        assert event.email == "orga@example.org"
        assert event.primary_color == "#12AbEF"
        assert event.is_public is True
        assert event.feature_flags == {
            **default_feature_flags(),
            "use_feedback": False,
            "show_schedule": True,
        }
        assert event.display_settings["html_export_url"] == ""
        assert not event.logo
        assert not event.custom_css
        intro = event.landing_page_text.localize(settings.LANGUAGE_CODE)
        assert "## Welcome" in intro
        assert "**friends**" in intro
        assert "[Details](https://frab.example.org/info)" in intro
        assert event.cfp.default_type.name.localize(settings.LANGUAGE_CODE) == "Talk"
        assert event.submission_types.count() == 1
        assert event.schedules.count() == 1
        assert event.wip_schedule.version is None
        assert event.rooms.count() == event.submissions.count() == 0
        assert event.review_phases.exists()
        assert ActivityLog.objects.filter(
            event=event, person=admin_user, action_type="pretalx.event.create"
        ).exists()
        assert not QueuedMail.objects.exists()
        assert response.url == settings_url
        team = organiser.teams.get(members=admin_user)
        assert team.can_change_event_settings
        assert team.can_change_submissions
        assert list(team.limit_events.all()) == [event]
    returned = admin_client.get(response.url)
    assert returned.status_code == 200
    assert b"The event was created. It is public." in returned.content
    assert returned.context["form"]["frab_migration_db_name"].value() == str(
        migration_database
    )
    assert BeautifulSoup(returned.content, "html.parser").find(
        "button", value="connect"
    )
    create.assert_called_once()
    post_create.assert_called_once_with(event, user=admin_user)
    assert create.call_args.kwargs["user"] == admin_user
    assert create.call_args.kwargs["organiser"] == organiser
    assert migration_database.read_bytes() == original
    assert GlobalSettings().settings.get("frab_migration_db_name") == str(
        migration_database
    )
    assert (
        GlobalSettings().settings.get("frab_migration_url")
        == "https://frab.example.org"
    )


@pytest.mark.parametrize(
    ("field", "value", "error_field"),
    (
        ("email", None, "email"),
        ("email", "invalid", "email"),
        ("title", "x" * 201, "name"),
        ("title", "", "name"),
        ("acronym", "not a slug", "slug"),
        ("acronym", "orga", "slug"),
        ("acronym", "x" * 51, "slug"),
        ("timezone", "Unknown/Zone", "timezone"),
        ("start_date", None, "date_from"),
        ("start_date", "bad date", "date_from"),
        ("end_date", "2026-10-20 12:00:00", "date_from"),
        ("color", "red", "primary_color"),
        ("feedback_enabled", None, "feedback_enabled"),
    ),
)
def test_invalid_source_creates_no_event(
    admin_client,
    settings_url,
    migration_database,
    migration_post,
    field,
    value,
    error_field,
):
    update_source(migration_database, **{field: value})
    response = admin_client.post(settings_url, migration_post)
    assert response.status_code == 200
    conference_form = response.context["conference_form"]
    assert conference_form.has_error(error_field) or error_field in str(
        conference_form.non_field_errors()
    )
    with scopes_disabled():
        assert not Event.objects.exists()
    assert not response.context["form"].is_bound


def test_duplicate_slug_does_not_update_an_event(
    admin_client, settings_url, migration_database, migration_post
):
    assert admin_client.post(settings_url, migration_post).status_code == 302
    update_source(migration_database, title="Changed source title")
    response = admin_client.post(settings_url, migration_post)
    assert response.status_code == 200
    assert "This short name is already taken." in str(
        response.context["conference_form"].non_field_errors()
    )
    assert b"ask the owner" not in response.content
    assert (
        BeautifulSoup(response.content, "html.parser")
        .find("button", value="migrate")
        .has_attr("disabled")
    )
    with scopes_disabled():
        assert Event.objects.count() == 1
        assert str(Event.objects.get().name) == "Alpha & Friends"


def test_native_creation_failure_rolls_back(
    admin_client, settings_url, migration_post, monkeypatch
):
    initialise = Mock(side_effect=IntegrityError("private database details"))
    monkeypatch.setattr(event_domain, "initialise_event", initialise)
    response = admin_client.post(settings_url, migration_post)
    assert response.status_code == 200
    assert b"Could not create the event" in response.content
    assert b"private database details" not in response.content
    initialise.assert_called_once()
    with scopes_disabled():
        assert not Event.objects.exists()


@pytest.mark.parametrize("selection", ("", "999", "42 OR 1=1"))
def test_migrate_requires_a_valid_selection(
    admin_client, settings_url, migration_post, selection
):
    response = admin_client.post(
        settings_url, {**migration_post, "conference": selection}
    )
    assert response.context["conference_form"].has_error("conference")
    with scopes_disabled():
        assert not Event.objects.exists()


@pytest.mark.parametrize(
    "change", ("missing", "modified", "expired", "database", "organiser", "user")
)
def test_migrate_requires_the_same_successful_connection(
    admin_client, admin_user, settings_url, migration_post, monkeypatch, change
):
    data = dict(migration_post)
    url = settings_url
    if change == "missing":
        data.pop("connection")
    elif change == "modified":
        data["connection"] += "invalid"
    elif change == "expired":
        monkeypatch.setattr(views, "CONNECTION_MAX_AGE", -1)
    elif change == "database":
        GlobalSettings().settings.set("frab_migration_db_name", "other-database")
    elif change == "organiser":
        organiser = Organiser.objects.create(name="Other", slug="other")
        url = reverse(
            "plugins:pretalx_frab_migration:settings",
            kwargs={"organiser": organiser.slug},
        )
    else:
        identity = signing.loads(data["connection"], salt=views.CONNECTION_SALT)
        identity[0] = admin_user.pk + 1
        data["connection"] = signing.dumps(identity, salt=views.CONNECTION_SALT)
    fetch = Mock()
    monkeypatch.setattr(views, "fetch_conferences", fetch)
    response = admin_client.post(url, data)
    assert b"Connect again" in response.content
    fetch.assert_not_called()
    with scopes_disabled():
        assert not Event.objects.exists()


@pytest.mark.parametrize("client_fixture", ("orga_client", "review_client", "client"))
def test_only_administrators_can_migrate(
    request, client_fixture, settings_url, monkeypatch
):
    client = request.getfixturevalue(client_fixture)
    migrate = Mock()
    monkeypatch.setattr(views, "migrate_conference", migrate)
    response = client.post(settings_url, {"action": "migrate", "conference": "42"})
    assert response.status_code == (302 if client_fixture == "client" else 404)
    migrate.assert_not_called()


def test_migrate_requires_csrf(admin_user, settings_url, migration_post):
    client = Client(enforce_csrf_checks=True)
    client.force_login(admin_user)
    assert client.post(settings_url, migration_post).status_code == 403


def test_migration_read_error_keeps_settings(
    admin_client, settings_url, migration_post, migration_database
):
    with closing(sqlite3.connect(migration_database)) as connection:
        connection.execute("ALTER TABLE conferences DROP COLUMN email")
        connection.commit()
    response = admin_client.post(settings_url, migration_post)
    assert b"selected conference could not be read" in response.content
    assert GlobalSettings().settings.get("frab_migration_db_name") == str(
        migration_database
    )
    with scopes_disabled():
        assert not Event.objects.exists()


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        ("Berlin", "Europe/Berlin"),
        ("Eastern Time (US & Canada)", "America/New_York"),
        ("UTC", "UTC"),
        ("Etc/UTC", "UTC"),
        ("Asia/Tokyo", "Asia/Tokyo"),
    ),
)
def test_timezone_conversion(source, expected):
    assert conference_timezone(source).key == expected


@pytest.mark.parametrize(
    "source",
    (
        "2026-10-22 00:30:00+02:00",
        dt.datetime(2026, 10, 21, 22, 30),
        dt.datetime(2026, 10, 21, 22, 30, tzinfo=dt.UTC),
    ),
)
def test_date_conversion_for_all_driver_types(source):
    assert conference_date(source, ZoneInfo("Europe/Berlin"), "date_from") == dt.date(
        2026, 10, 22
    )


def test_html_intro_preserves_content_and_removes_active_markup():
    html = '<style>hidden-style</style><script>hidden-script</script><h1>Title</h1><ul><li>Item</li></ul><a href="javascript:alert(1)">Unsafe</a><a href="relative">Link</a><img src="/image.png" alt="Image"><a href="mailto:test@example.org">Email</a>'
    result = conference_intro(html, "https://frab.example.org/base")
    assert "hidden" not in result
    assert "javascript" not in result
    assert "# Title" in result
    assert "* Item" in result
    assert "Unsafe" in result
    assert "[Link](https://frab.example.org/base/relative)" in result
    assert "![Image](https://frab.example.org/image.png)" in result
    assert "mailto:test@example.org" in result


def test_empty_optional_fields_use_native_defaults(
    organiser, admin_user, migration_database
):
    source = fetch_conference(42, backend="sqlite3", name=str(migration_database))
    source.update(color=None, schedule_html_intro=None)
    event = migrate_conference(source, organiser=organiser, user=admin_user)
    assert event.primary_color is None
    assert not str(event.landing_page_text)


@pytest.mark.parametrize(
    ("value", "expected"),
    ((0, False), (1, True), (False, False), (True, True), ("0", False), ("1", True)),
)
def test_driver_boolean_values(value, expected):
    assert conference_boolean(value, "test") is expected


def test_invalid_boolean_reports_a_field_error():
    with pytest.raises(ValidationError, match="true or false"):
        conference_boolean("invalid", "test")


@pytest.mark.parametrize(
    ("source_field", "target_field", "value"),
    (
        ("email", "email", "replacement@example.org"),
        ("title", "name", "Replacement name"),
        ("acronym", "slug", "replacement-slug"),
        ("timezone", "timezone", "Europe/Paris"),
        ("start_date", "date_from", "2026-10-22"),
        ("end_date", "date_to", "2026-10-24"),
    ),
)
def test_missing_required_values_are_requested_and_used(
    admin_client,
    settings_url,
    migration_database,
    migration_post,
    source_field,
    target_field,
    value,
):
    update_source(migration_database, **{source_field: None})
    original = migration_database.read_bytes()
    response = admin_client.post(settings_url, {**migration_post, "action": "select"})
    form = response.context["conference_form"]
    assert form.missing_fields == [target_field]
    assert not form.is_bound
    assert not form.errors
    html = response.content.decode()
    assert (
        html.index('name="conference"')
        < html.index(f'name="{target_field}"')
        < html.index('value="migrate"')
    )
    with scopes_disabled():
        assert not Event.objects.exists()
    response = admin_client.post(settings_url, {**migration_post, target_field: value})
    assert response.status_code == 302
    with scopes_disabled():
        event = Event.objects.get()
        assert str(getattr(event, target_field)) == value
    assert migration_database.read_bytes() == original


def test_multiple_missing_fields_keep_values_after_validation_error(
    admin_client, settings_url, migration_database, migration_post
):
    update_source(migration_database, email=" ", start_date=None, end_date=None)
    response = admin_client.post(
        settings_url,
        {
            **migration_post,
            "email": "replacement@example.org",
            "date_from": "2026-10-25",
            "date_to": "2026-10-24",
        },
    )
    form = response.context["conference_form"]
    assert form.has_error("date_from")
    assert form["email"].value() == "replacement@example.org"
    with scopes_disabled():
        assert not Event.objects.exists()


def test_complete_source_does_not_accept_overrides(
    admin_client, settings_url, migration_post
):
    response = admin_client.post(
        settings_url,
        {
            **migration_post,
            "email": "unwanted@example.org",
            "organiser": "other",
            "is_public": "false",
        },
    )
    assert response.status_code == 302
    with scopes_disabled():
        event = Event.objects.get()
        assert event.email == "orga@example.org"
        assert event.is_public


def test_post_creation_failure_rolls_back_event_and_team(
    admin_client, admin_user, organiser, settings_url, migration_post, monkeypatch
):
    with scopes_disabled():
        previous_teams = set(organiser.teams.values_list("pk", flat=True))

    def fail_after_post_create(event, *, user):
        event_domain.post_create_event(event, user=user)
        assert organiser.teams.filter(members=admin_user, limit_events=event).exists()
        raise IntegrityError("private database details")

    post_create = Mock(side_effect=fail_after_post_create)
    monkeypatch.setattr(migration, "post_create_event", post_create)
    response = admin_client.post(settings_url, migration_post)

    assert response.status_code == 200
    assert b"Could not create the event" in response.content
    assert b"private database details" not in response.content
    post_create.assert_called_once()
    with scopes_disabled():
        assert not Event.objects.exists()
        assert not ActivityLog.objects.filter(
            action_type="pretalx.event.create"
        ).exists()
        assert set(organiser.teams.values_list("pk", flat=True)) == previous_teams


def test_post_creation_reuses_a_team_with_access_to_all_events(
    admin_client, admin_user, organiser, settings_url, migration_post
):
    with scopes_disabled():
        team = organiser.teams.get(can_change_event_settings=True)
        team.all_events = True
        team.save(update_fields=["all_events"])
        team.members.add(admin_user)
        previous_teams = set(organiser.teams.values_list("pk", flat=True))

    response = admin_client.post(settings_url, migration_post)

    assert response.status_code == 302
    with scopes_disabled():
        assert Event.objects.count() == 1
        assert set(organiser.teams.values_list("pk", flat=True)) == previous_teams
        assert list(admin_user.teams.all()) == [team]


@pytest.mark.parametrize(
    ("conference_email", "expected"),
    (
        (None, "cfp@example.org"),
        ("", "cfp@example.org"),
        ("  ", "cfp@example.org"),
        ("orga@example.org", "orga@example.org"),
    ),
)
def test_cfp_email_is_used_only_when_the_conference_email_is_missing(
    admin_client,
    settings_url,
    migration_database,
    migration_post,
    conference_email,
    expected,
):
    update_source(migration_database, email=conference_email)
    with closing(sqlite3.connect(migration_database)) as connection:
        connection.execute("""
            INSERT INTO call_for_participations (conference_id, contact_email)
            VALUES (42, ' cfp@example.org '), (7, 'other@example.org')
        """)
        connection.commit()
    original = migration_database.read_bytes()

    response = admin_client.post(settings_url, {**migration_post, "action": "select"})
    assert "email" not in response.context["conference_form"].fields
    response = admin_client.post(settings_url, migration_post)

    assert response.status_code == 302
    with scopes_disabled():
        assert Event.objects.get().email == expected
    assert migration_database.read_bytes() == original


@pytest.mark.parametrize("cfp_email", (None, "", " ", "no-cfp-row"))
def test_missing_conference_and_cfp_emails_request_user_input(
    admin_client, settings_url, migration_database, migration_post, cfp_email
):
    update_source(migration_database, email=None)
    with closing(sqlite3.connect(migration_database)) as connection:
        connection.execute("""
            INSERT INTO call_for_participations (conference_id, contact_email)
            VALUES (7, 'other@example.org')
        """)
        if cfp_email != "no-cfp-row":
            connection.execute(
                "INSERT INTO call_for_participations (conference_id, contact_email) VALUES (?, ?)",
                (42, cfp_email),
            )
        connection.commit()
    original = migration_database.read_bytes()

    response = admin_client.post(settings_url, {**migration_post, "action": "select"})
    assert response.context["conference_form"].fields["email"].required
    response = admin_client.post(
        settings_url, {**migration_post, "email": "manual@example.org"}
    )

    assert response.status_code == 302
    with scopes_disabled():
        assert Event.objects.get().email == "manual@example.org"
    assert migration_database.read_bytes() == original


@pytest.mark.parametrize("schedule_public", (None, 0, 1))
def test_event_is_public_without_a_source_visibility_setting(
    admin_client, settings_url, migration_post, migration_database, schedule_public
):
    if schedule_public is not None:
        with closing(sqlite3.connect(migration_database)) as connection:
            connection.execute(
                "ALTER TABLE conferences ADD COLUMN schedule_public BOOLEAN"
            )
            connection.execute(
                "UPDATE conferences SET schedule_public = ?", (schedule_public,)
            )
            connection.commit()

    response = admin_client.post(settings_url, migration_post)

    assert response.status_code == 302
    with scopes_disabled():
        event = Event.objects.get()
        assert event.is_public is True
        assert event.feature_flags["show_schedule"] is True
        assert event.current_schedule is None
        event_url = str(event.urls.base)
    public_response = Client().get(event_url, follow=True)
    assert public_response.status_code == 200


@pytest.mark.parametrize("other_organiser", (False, True))
def test_selection_detects_slug_conflicts_and_blocks_migration(
    admin_client,
    settings_url,
    migration_database,
    migration_post,
    monkeypatch,
    other_organiser,
):
    assert admin_client.post(settings_url, migration_post).status_code == 302
    if other_organiser:
        with scopes_disabled():
            other = Organiser.objects.create(name="Other", slug="other")
            Event.objects.update(organiser=other)
    update_source(migration_database, email=None)
    migrate = Mock()
    monkeypatch.setattr(views, "migrate_conference", migrate)

    response = admin_client.post(settings_url, {**migration_post, "action": "select"})

    form = response.context["conference_form"]
    assert form.event_slug == "af-2026"
    assert (
        str(form.slug_error)
        == "This short name is already taken. Please choose another one."
    )
    assert not form.is_bound
    assert not form.errors
    assert "email" in form.fields
    soup = BeautifulSoup(response.content, "html.parser")
    assert soup.find("button", value="migrate").has_attr("disabled")
    assert soup.find("div", role="alert").get_text(strip=True) == str(form.slug_error)
    assert b"ask the owner" not in response.content

    response = admin_client.post(
        settings_url, {**migration_post, "email": "manual@example.org"}
    )
    assert response.status_code == 200
    migrate.assert_not_called()
    with scopes_disabled():
        assert Event.objects.count() == 1


def test_selecting_a_free_conference_clears_the_conflict(
    admin_client, settings_url, migration_database, migration_post
):
    assert admin_client.post(settings_url, migration_post).status_code == 302
    response = admin_client.post(settings_url, {**migration_post, "action": "select"})
    assert response.context["conference_form"].slug_error
    with closing(sqlite3.connect(migration_database)) as connection:
        connection.execute(
            "INSERT INTO conferences (id, title, acronym) VALUES (7, 'Other', 'OTHER')"
        )
        connection.commit()

    response = admin_client.post(
        settings_url, {**migration_post, "action": "select", "conference": "7"}
    )

    assert response.context["conference_form"].slug_error is None
    assert b"This short name is already taken" not in response.content
    assert (
        not BeautifulSoup(response.content, "html.parser")
        .find("button", value="migrate")
        .has_attr("disabled")
    )


@pytest.mark.parametrize("error_type", ("validation", "integrity"))
def test_conflict_during_creation_uses_the_same_message(
    admin_client, settings_url, migration_post, monkeypatch, error_type
):
    def create_conflicting_event(conference, **kwargs):
        migrate_conference(conference, **kwargs)
        if error_type == "integrity":
            raise IntegrityError("private database details")
        return migrate_conference(conference, **kwargs)

    monkeypatch.setattr(views, "migrate_conference", create_conflicting_event)
    response = admin_client.post(settings_url, migration_post)

    assert response.status_code == 200
    assert (
        b"This short name is already taken. Please choose another one."
        in response.content
    )
    assert b"ask the owner" not in response.content
    assert b"private database details" not in response.content
    assert (
        BeautifulSoup(response.content, "html.parser")
        .find("button", value="migrate")
        .has_attr("disabled")
    )
    with scopes_disabled():
        assert Event.objects.count() == 1


def test_manual_slug_can_be_checked_and_corrected_without_losing_other_values(
    admin_client, settings_url, migration_database, migration_post
):
    assert admin_client.post(settings_url, migration_post).status_code == 302
    update_source(migration_database, acronym=None, email=None)
    data = {
        **migration_post,
        "action": "check_slug",
        "slug": "AF-2026",
        "email": "manual@example.org",
    }

    response = admin_client.post(settings_url, data)

    form = response.context["conference_form"]
    assert form.slug_error
    assert form["email"].value() == "manual@example.org"
    assert not form.errors
    assert (
        BeautifulSoup(response.content, "html.parser")
        .find("button", value="migrate")
        .has_attr("disabled")
    )
    data["slug"] = "new-short-name"
    response = admin_client.post(settings_url, data)
    assert response.context["conference_form"].slug_error is None
    assert (
        not BeautifulSoup(response.content, "html.parser")
        .find("button", value="migrate")
        .has_attr("disabled")
    )
    response = admin_client.post(settings_url, {**data, "action": "migrate"})
    assert response.status_code == 302
    with scopes_disabled():
        assert Event.objects.get(slug="new-short-name").email == "manual@example.org"


@pytest.mark.parametrize(
    "source_url",
    (
        None,
        "",
        "https://frab.example.org/export/",
        "https://other.example.org/old-schedule",
    ),
)
def test_export_url_is_copied_without_conditions(
    admin_client, settings_url, migration_database, migration_post, source_url
):
    update_source(migration_database, program_export_base_url=source_url)
    original = migration_database.read_bytes()

    response = admin_client.post(settings_url, migration_post)

    assert response.status_code == 302
    with scopes_disabled():
        event = Event.objects.get()
        assert event.display_settings == {
            **default_display_settings(),
            "html_export_url": source_url or "",
        }
    assert migration_database.read_bytes() == original
