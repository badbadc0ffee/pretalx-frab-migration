import colorsys
import datetime as dt
import sqlite3
from contextlib import closing
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from bs4 import BeautifulSoup
from django.conf import settings
from django.contrib import messages
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import Client
from django.urls import reverse
from django_scopes import scopes_disabled

from pretalx.common.models import ActivityLog
from pretalx.common.models.settings import GlobalSettings
from pretalx.common.ui import _relative_luminance
from pretalx.common.views.redirect import safelink
from pretalx.event.domain import event as event_domain
from pretalx.event.models import Event, Organiser
from pretalx.event.models.event import default_display_settings, default_feature_flags
from pretalx.mail.models import QueuedMail
from pretalx.submission.interfaces.forms import InfoForm
from pretalx.submission.models import CfP, SubmissionType, Track

from pretalx_frab_migration import migration, views
from pretalx_frab_migration.database import fetch_conference
from pretalx_frab_migration.migration import (
    conference_boolean,
    conference_date,
    conference_intro,
    conference_timezone,
    migrate_conference,
    track_color,
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
        "import_tracks": "on",
        "import_submission_types": "on",
        "conference": "42",
        "connection": response.context["conference_form"].initial["connection"],
    }


def test_migrate_creates_the_event_with_native_initialisation(
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
        assert event.cfp.opening is None
        assert event.cfp.deadline is None
        assert not event.cfp.text
        assert event.submission_types.count() == 2
        assert event.cfp.default_type.default_duration == 45
        assert event.schedules.count() == 1
        assert event.wip_schedule.version is None
        assert (
            event.rooms.count()
            == event.submissions.count()
            == event.tracks.count()
            == 0
        )
        assert event.cfp.fields["track"]["visibility"] == "do_not_ask"
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
    assert any(
        notice.level == messages.ERROR and str(notice) == str(form.slug_error)
        for notice in response.context["messages"]
    )
    assert str(form.slug_error) in soup.get_text()
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


@pytest.fixture
def frab_cfp(migration_database):
    with closing(sqlite3.connect(migration_database)) as connection:
        connection.execute(
            "INSERT INTO call_for_participations "
            "(conference_id, start_date, end_date, hard_deadline, welcome_text, info_url, contact_email) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                42,
                "2020-03-28",
                "2020-03-28",
                "2020-03-29",
                '<h2>Submit a talk</h2><p>Read the <a href="/guide">guide</a>.</p><script>unsafe-script</script>',
                "/cfp-info",
                "speakers@example.org",
            ),
        )
        connection.execute(
            "INSERT INTO call_for_participations (conference_id, welcome_text) VALUES (?, ?)",
            (7, "Other conference text"),
        )
        connection.commit()
    return migration_database


@pytest.mark.parametrize("hard_deadline", (None, "", "2020-03-29"))
def test_cfp_content_dates_and_submission_window_are_migrated(
    admin_client, settings_url, migration_post, frab_cfp, monkeypatch, hard_deadline
):
    with closing(sqlite3.connect(frab_cfp)) as connection:
        connection.execute(
            "UPDATE call_for_participations SET hard_deadline = ? WHERE conference_id = 42",
            (hard_deadline,),
        )
        connection.commit()
    original = frab_cfp.read_bytes()
    response = admin_client.post(settings_url, migration_post)
    assert response.status_code == 302
    assert response.url == settings_url
    with scopes_disabled():
        event = Event.objects.get()
        cfp = event.cfp
        assert event.email == "orga@example.org"
        assert cfp.opening == dt.datetime(2020, 3, 27, 23, tzinfo=dt.UTC)
        if hard_deadline:
            assert cfp.deadline == dt.datetime(
                2020, 3, 29, 21, 59, 59, 999999, tzinfo=dt.UTC
            )
        else:
            assert cfp.deadline == dt.datetime(
                2020, 3, 28, 22, 59, 59, 999999, tzinfo=dt.UTC
            )
        text = cfp.text.localize(event.locale)
        assert (
            text
            == "## Submit a talk\n\nRead the [guide](https://frab.example.org/guide)."
        )
        cfp_url = str(cfp.urls.public)
        for moment, expected in (
            (dt.datetime(2020, 3, 27, 22, 59, 59, tzinfo=dt.UTC), False),
            (dt.datetime(2020, 3, 27, 23, tzinfo=dt.UTC), True),
            (dt.datetime(2020, 3, 28, 22, 59, 59, tzinfo=dt.UTC), True),
            (dt.datetime(2020, 3, 28, 23, tzinfo=dt.UTC), bool(hard_deadline)),
            (dt.datetime(2020, 3, 29, 21, 59, 59, tzinfo=dt.UTC), bool(hard_deadline)),
            (dt.datetime(2020, 3, 29, 22, tzinfo=dt.UTC), False),
        ):
            monkeypatch.setattr(
                "pretalx.submission.models.cfp.now", lambda moment=moment: moment
            )
            assert Event.objects.get().cfp.is_open is expected
    public_response = Client().get(cfp_url)
    assert public_response.status_code == 200
    assert b"Submit a talk" in public_response.content
    assert b"mailto:speakers@example.org" not in public_response.content
    assert b"cfp-info" not in public_response.content
    assert BeautifulSoup(public_response.content, "html.parser").find(
        "a", href=safelink("https://frab.example.org/guide")
    )
    assert b"unsafe-script" not in public_response.content
    assert frab_cfp.read_bytes() == original


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("start_date", "bad-date"),
        ("end_date", "bad-date"),
        ("hard_deadline", "bad-date"),
        ("end_date", "2020-03-27"),
        ("hard_deadline", "2020-03-27"),
    ),
)
def test_invalid_cfp_rolls_back_event_and_organiser_access(
    admin_client, settings_url, migration_post, frab_cfp, organiser, field, value
):
    with closing(sqlite3.connect(frab_cfp)) as connection:
        connection.execute(
            f"UPDATE call_for_participations SET {field} = ? WHERE conference_id = 42",
            (value,),
        )
        if field == "end_date":
            connection.execute(
                "UPDATE call_for_participations SET hard_deadline = NULL WHERE conference_id = 42"
            )
        connection.commit()
    with scopes_disabled():
        team_count = organiser.teams.count()
    response = admin_client.post(settings_url, migration_post)
    assert response.status_code == 200
    assert f"cfp_{field}" in str(response.context["conference_form"].non_field_errors())
    with scopes_disabled():
        assert not Event.objects.exists()
        assert organiser.teams.count() == team_count


def test_cfp_accepts_native_database_dates(frab_cfp, organiser, admin_user):
    source = fetch_conference(42, backend="sqlite3", name=str(frab_cfp))
    for field in ("start_date", "end_date", "hard_deadline"):
        source["cfp"][field] = dt.date.fromisoformat(source["cfp"][field])
    event = migrate_conference(
        source,
        organiser=organiser,
        user=admin_user,
        source_url="https://frab.example.org",
    )
    with scopes_disabled():
        event = Event.objects.get(pk=event.pk)
        assert event.cfp.opening.astimezone(event.tz).date() == dt.date(2020, 3, 28)
        assert event.cfp.deadline.astimezone(event.tz).date() == dt.date(2020, 3, 29)


@pytest.mark.parametrize("welcome_text", (None, "", "  \n  "))
def test_missing_cfp_text_stays_empty(
    admin_client, settings_url, migration_post, frab_cfp, welcome_text
):
    with closing(sqlite3.connect(frab_cfp)) as connection:
        connection.execute(
            "UPDATE call_for_participations SET welcome_text = ? WHERE conference_id = 42",
            (welcome_text,),
        )
        connection.commit()
    assert admin_client.post(settings_url, migration_post).status_code == 302
    with scopes_disabled():
        assert not Event.objects.get().cfp.text.localize(settings.LANGUAGE_CODE)


def test_cfp_ignores_unused_metadata_and_backup_date(
    admin_client, settings_url, migration_post, frab_cfp
):
    with closing(sqlite3.connect(frab_cfp)) as connection:
        connection.execute("ALTER TABLE call_for_participations DROP COLUMN info_url")
        connection.execute(
            "UPDATE call_for_participations SET end_date = 'invalid-date', "
            "contact_email = 'invalid-email' WHERE conference_id = 42"
        )
        connection.commit()
    assert admin_client.post(settings_url, migration_post).status_code == 302
    with scopes_disabled():
        event = Event.objects.get()
        assert event.email == "orga@example.org"
        assert event.cfp.deadline.astimezone(event.tz).date() == dt.date(2020, 3, 29)


def test_cfp_has_no_deadline_when_both_source_dates_are_missing(
    admin_client, settings_url, migration_post, frab_cfp
):
    with closing(sqlite3.connect(frab_cfp)) as connection:
        connection.execute(
            "UPDATE call_for_participations SET hard_deadline = NULL, end_date = NULL "
            "WHERE conference_id = 42"
        )
        connection.commit()
    assert admin_client.post(settings_url, migration_post).status_code == 302
    with scopes_disabled():
        assert Event.objects.get().cfp.deadline is None


@pytest.fixture
def frab_tracks(migration_database):
    with closing(sqlite3.connect(migration_database)) as connection:
        connection.executemany(
            "INSERT INTO tracks (id, conference_id, name, color) VALUES (?, ?, ?, ?)",
            (
                (20, 42, "Zeta", "ABC"),
                (10, 42, "Alpha", "12AbEF"),
                (30, 42, "No colour", None),
                (40, 7, "Other conference", "000000"),
            ),
        )
        connection.execute(
            "INSERT INTO conferences (id, title, acronym, parent_id) VALUES (7, 'Child', 'CHILD', 42)"
        )
        connection.commit()
    return migration_database


def test_tracks_migrate_in_source_order_and_are_available_in_the_cfp(
    admin_client, settings_url, migration_post, frab_tracks
):
    original = frab_tracks.read_bytes()
    response = admin_client.post(settings_url, migration_post)
    assert response.status_code == 302
    assert response.url == settings_url
    with scopes_disabled():
        event = Event.objects.get()
        assert event.feature_flags["use_tracks"] is True
        tracks = list(event.tracks.all())
        assert [(str(track.name), track.color, track.position) for track in tracks] == [
            ("Alpha", "#2FB0EA", 0),
            ("No colour", "#ABAA22", 1),
            ("Zeta", "#297ACC", 2),
        ]
        assert Track.objects.count() == 3
        assert all(not track.description for track in tracks)
        assert all(not track.requires_access_code for track in tracks)
        assert all(not track.attendee_signup_required for track in tracks)
        form = InfoForm(event=event)
        assert not form.fields["track"].required
        assert list(form.fields["track"].queryset) == tracks
        assert event.submissions.count() == 0
    assert frab_tracks.read_bytes() == original


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("name", None),
        ("name", ""),
        ("name", "x" * 201),
        ("name", "Alpha"),
        ("color", "red"),
        ("color", "#1234"),
        ("color", "1234567"),
    ),
)
def test_invalid_track_rolls_back_the_complete_migration(
    admin_client, settings_url, migration_post, frab_tracks, organiser, field, value
):
    with closing(sqlite3.connect(frab_tracks)) as connection:
        connection.execute(f"UPDATE tracks SET {field} = ? WHERE id = 20", (value,))
        connection.commit()
    with scopes_disabled():
        team_count = organiser.teams.count()
    response = admin_client.post(settings_url, migration_post)
    assert response.status_code == 200
    assert "Could not migrate frab track 20" in str(
        response.context["conference_form"].non_field_errors()
    )
    with scopes_disabled():
        assert not Event.objects.exists()
        assert not Track.objects.exists()
        assert organiser.teams.count() == team_count


@pytest.mark.parametrize(
    ("color", "expected"),
    (("", "#ABAA22"), ("  ", "#ABAA22"), (" #123456 ", "#1D5791")),
)
def test_track_colour_normalisation(
    admin_client, settings_url, migration_post, frab_tracks, color, expected
):
    with closing(sqlite3.connect(frab_tracks)) as connection:
        connection.execute("UPDATE tracks SET color = ? WHERE id = 20", (color,))
        connection.commit()
    assert admin_client.post(settings_url, migration_post).status_code == 302
    with scopes_disabled():
        assert str(Track.objects.get(name__icontains="Zeta").color) == expected


@pytest.mark.parametrize("hue", (0, 1 / 6, 1 / 3, 1 / 2, 2 / 3, 5 / 6))
@pytest.mark.parametrize("value", (0.2, 0.6, 1.0))
def test_track_colour_preserves_hue_and_clamps_value_to_good_contrast(hue, value):
    source_rgb = tuple(
        round(channel * 255) for channel in colorsys.hsv_to_rgb(hue, 0.3, value)
    )
    source = "#" + "".join(f"{channel:02X}" for channel in source_rgb)
    source_hue, _, source_value = colorsys.rgb_to_hsv(
        *(channel / 255 for channel in source_rgb)
    )
    result = track_color(source)
    rgb = tuple(int(result[index : index + 2], 16) / 255 for index in (1, 3, 5))
    result_hue, saturation, result_value = colorsys.rgb_to_hsv(*rgb)
    difference = abs(result_hue - source_hue)
    assert min(difference, 1 - difference) < 1 / 360
    assert saturation == pytest.approx(0.8, abs=0.006)

    def readable(channels):
        luminance = _relative_luminance(*channels)
        dark = _relative_luminance(18 / 255, 20 / 255, 22 / 255)
        return (
            round(1.05 / (luminance + 0.05), 1) >= 2.5
            and round((luminance + 0.05) / (dark + 0.05), 1) >= 2.5
        )

    assert readable(rgb)
    if result_value != source_value:
        direction = 1 if source_value > result_value else -1
        next_value = (round(result_value * 255) + direction) / 255
        next_rgb = tuple(
            round(channel * 255) / 255
            for channel in colorsys.hsv_to_rgb(source_hue, 0.8, next_value)
        )
        assert not readable(next_rgb)


@pytest.mark.parametrize(
    ("source", "expected"), (("#000000", "#A32121"), ("#FFFFFF", "#FF3333"))
)
def test_track_colour_uses_hsv_zero_hue_for_grey(source, expected):
    assert track_color(source) == expected


@pytest.mark.parametrize("track_count", (0, 1, 2))
def test_track_checkbox_is_shown_only_for_multiple_tracks(
    admin_client, settings_url, migration_post, frab_tracks, track_count
):
    with closing(sqlite3.connect(frab_tracks)) as connection:
        connection.execute(
            "DELETE FROM tracks WHERE conference_id = 42 AND id NOT IN "
            "(SELECT id FROM tracks WHERE conference_id = 42 ORDER BY id LIMIT ?)",
            (track_count,),
        )
        connection.commit()
    # A conference selection must restore the default, even after a cleared checkbox.
    data = {
        key: value for key, value in migration_post.items() if key != "import_tracks"
    }
    response = admin_client.post(settings_url, {**data, "action": "select"})
    assert response.status_code == 200
    soup = BeautifulSoup(response.content, "html.parser")
    checkbox = soup.find("input", attrs={"name": "import_tracks"})
    if track_count > 1:
        assert checkbox["type"] == "checkbox"
        assert checkbox.has_attr("checked")
        assert not checkbox.has_attr("required")
        assert checkbox.find_parent("form").find("button", value="migrate")
    else:
        assert checkbox is None
        response = admin_client.post(settings_url, data)
        assert response.status_code == 302
        with scopes_disabled():
            event = Event.objects.get()
            assert event.tracks.count() == track_count
            assert event.feature_flags["use_tracks"] is True


@pytest.mark.parametrize("import_tracks", (False, True))
@pytest.mark.parametrize("action", ("check_slug", "migrate"))
def test_track_choice_survives_slug_checks_and_form_errors(
    admin_client, settings_url, migration_post, frab_tracks, import_tracks, action
):
    update_source(frab_tracks, acronym=None, email=None)
    data = {**migration_post, "action": action, "slug": "manual-slug"}
    if not import_tracks:
        del data["import_tracks"]
    response = admin_client.post(settings_url, data)
    assert response.status_code == 200
    form = response.context["conference_form"]
    checkbox = BeautifulSoup(response.content, "html.parser").find(
        "input", attrs={"name": "import_tracks"}
    )
    assert checkbox.has_attr("checked") is import_tracks
    if action == "migrate":
        assert form.has_error("email")
    with scopes_disabled():
        assert not Event.objects.exists()


@pytest.mark.parametrize("invalid_tracks", (False, True))
def test_unchecked_tracks_are_skipped_and_the_feature_is_disabled(
    admin_client, settings_url, migration_post, frab_tracks, frab_cfp, invalid_tracks
):
    if invalid_tracks:
        with closing(sqlite3.connect(frab_tracks)) as connection:
            connection.execute(
                "UPDATE tracks SET name = NULL, color = 'invalid' WHERE conference_id = 42"
            )
            connection.commit()
    original = frab_tracks.read_bytes()
    data = {
        key: value for key, value in migration_post.items() if key != "import_tracks"
    }
    response = admin_client.post(settings_url, data)
    assert response.status_code == 302
    assert response.url == settings_url
    with scopes_disabled():
        event = Event.objects.get()
        assert event.feature_flags["use_tracks"] is False
        assert not Track.objects.exists()
        assert event.cfp.fields["track"]["visibility"] == "do_not_ask"
        assert "track" not in InfoForm(event=event).fields
        assert event.cfp.deadline.astimezone(event.tz).date() == dt.date(2020, 3, 29)
        assert "Submit a talk" in str(event.cfp.text)
    assert frab_tracks.read_bytes() == original


@pytest.mark.parametrize(
    ("source_types", "expected_names", "default_name"),
    (
        (
            "workshop; lecture; panel & Q&A;lecture;Talk;Workshop;djset;DJ Set;lightning_talk;; ",
            ["Talk", "Workshop", "Panel & Q&A", "DJ Set", "Lightning Talk"],
            "Talk",
        ),
        ("lecture", ["Talk"], "Talk"),
        ("workshop;Seminar", ["Workshop", "Seminar"], "Workshop"),
        (None, ["Talk"], "Talk"),
        ("", ["Talk"], "Talk"),
        (" ; ; ", ["Talk"], "Talk"),
    ),
)
def test_session_types_use_source_names_durations_and_the_native_default(
    admin_client,
    settings_url,
    migration_database,
    migration_post,
    monkeypatch,
    source_types,
    expected_names,
    default_name,
):
    update_source(migration_database, allowed_event_types=source_types)
    original = migration_database.read_bytes()
    native_defaults = []

    def post_create(event, *, user):
        native_defaults.append(event.cfp.default_type_id)
        return event_domain.post_create_event(event, user=user)

    monkeypatch.setattr(migration, "post_create_event", post_create)
    response = admin_client.post(settings_url, migration_post)
    assert response.status_code == 302
    assert response.url == settings_url
    with scopes_disabled():
        event = Event.objects.get()
        types = list(event.submission_types.order_by("pk"))
        assert [str(item.name) for item in types] == expected_names
        assert event.cfp.default_type_id == native_defaults[0]
        assert str(event.cfp.default_type.name) == default_name
        assert all(item.default_duration == 45 for item in types)
        assert all(item.deadline is None for item in types)
        assert all(not item.requires_access_code for item in types)
        assert all(not item.attendee_signup_required for item in types)
        form = InfoForm(event=event)
        if len(types) > 1:
            assert set(form.fields["submission_type"].queryset) == set(types)
            assert form.fields["submission_type"].required
        else:
            assert "submission_type" not in form.fields
        assert not event.submissions.exists()
    assert migration_database.read_bytes() == original


@pytest.mark.parametrize(
    ("source_types", "shown"),
    (
        (None, False),
        ("lecture", False),
        ("lecture; lecture; ", False),
        ("lecture;workshop", True),
    ),
)
def test_session_type_checkbox_is_selected_only_for_multiple_source_types(
    admin_client, settings_url, migration_database, migration_post, source_types, shown
):
    update_source(migration_database, allowed_event_types=source_types)
    data = {
        key: value
        for key, value in migration_post.items()
        if key != "import_submission_types"
    }
    response = admin_client.post(settings_url, {**data, "action": "select"})
    assert response.status_code == 200
    soup = BeautifulSoup(response.content, "html.parser")
    checkbox = soup.find("input", attrs={"name": "import_submission_types"})
    if shown:
        assert checkbox["type"] == "checkbox"
        assert checkbox.has_attr("checked")
        assert not checkbox.has_attr("required")
    else:
        assert checkbox is None
    with scopes_disabled():
        assert not Event.objects.exists()


@pytest.mark.parametrize("selected", (False, True))
@pytest.mark.parametrize("action", ("check_slug", "migrate"))
def test_session_type_choice_survives_slug_checks_and_form_errors(
    admin_client, settings_url, migration_database, migration_post, selected, action
):
    update_source(migration_database, acronym=None, email=None)
    data = {**migration_post, "action": action, "slug": "manual-slug"}
    if not selected:
        del data["import_submission_types"]
    response = admin_client.post(settings_url, data)
    assert response.status_code == 200
    checkbox = BeautifulSoup(response.content, "html.parser").find(
        "input", attrs={"name": "import_submission_types"}
    )
    assert checkbox.has_attr("checked") is selected
    if action == "migrate":
        assert response.context["conference_form"].has_error("email")
    with scopes_disabled():
        assert not Event.objects.exists()


@pytest.mark.parametrize("import_tracks", (False, True))
@pytest.mark.parametrize("import_types", (False, True))
def test_track_and_session_type_import_options_are_independent(
    admin_client,
    settings_url,
    migration_post,
    frab_tracks,
    frab_cfp,
    import_tracks,
    import_types,
):
    data = migration_post.copy()
    if not import_tracks:
        del data["import_tracks"]
    if not import_types:
        del data["import_submission_types"]
        update_source(
            frab_tracks,
            allowed_event_types="lecture;" + "x" * 101,
            timeslot_duration=None,
            default_timeslots=-1,
        )
    original = frab_tracks.read_bytes()
    response = admin_client.post(settings_url, data)
    assert response.status_code == 302
    with scopes_disabled():
        event = Event.objects.get()
        assert str(event.cfp.default_type.name) == "Talk"
        assert event.cfp.default_type.default_duration == (45 if import_types else 30)
        assert event.submission_types.count() == (2 if import_types else 1)
        assert event.tracks.count() == (3 if import_tracks else 0)
        assert event.feature_flags["use_tracks"] is import_tracks
        assert "Submit a talk" in str(event.cfp.text)
    assert frab_tracks.read_bytes() == original


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("allowed_event_types", "lecture;" + "x" * 101),
        ("timeslot_duration", None),
        ("timeslot_duration", 0),
        ("timeslot_duration", "bad"),
        ("default_timeslots", None),
        ("default_timeslots", -1),
        ("default_timeslots", "1.5"),
        ("default_timeslots", 10**18),
    ),
)
def test_invalid_session_types_roll_back_the_complete_import(
    admin_client,
    settings_url,
    migration_post,
    frab_tracks,
    frab_cfp,
    organiser,
    field,
    value,
):
    update_source(frab_tracks, **{field: value})
    with scopes_disabled():
        team_count = organiser.teams.count()
    response = admin_client.post(settings_url, migration_post)
    assert response.status_code == 200
    assert response.context["conference_form"].non_field_errors()
    with scopes_disabled():
        assert not Event.objects.exists()
        assert not CfP.objects.exists()
        assert not Track.objects.exists()
        assert not SubmissionType.objects.exists()
        assert organiser.teams.count() == team_count


def test_child_session_types_use_own_names_and_count_with_inherited_slot_duration(
    admin_client, settings_url, migration_database, migration_post
):
    with closing(sqlite3.connect(migration_database)) as connection:
        connection.execute(
            "INSERT INTO conferences "
            "(id, title, acronym, email, feedback_enabled, parent_id, "
            "allowed_event_types, default_timeslots, timeslot_duration) "
            "VALUES (7, 'Child', 'CHILD', 'child@example.org', 0, 42, 'lecture;Tutorial', 4, 99)"
        )
        connection.commit()
    original = migration_database.read_bytes()
    response = admin_client.post(settings_url, {**migration_post, "conference": "7"})
    assert response.status_code == 302
    with scopes_disabled():
        event = Event.objects.get()
        assert event.slug == "child"
        types = list(event.submission_types.order_by("pk"))
        assert [(str(item.name), item.default_duration) for item in types] == [
            ("Talk", 60),
            ("Tutorial", 60),
        ]
        assert event.cfp.default_type == types[0]
    assert migration_database.read_bytes() == original
