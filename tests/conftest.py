import datetime as dt
import sqlite3
from contextlib import closing

import pytest
from django.core import management
from django.core.cache import caches
from django.urls import reverse
from django_scopes import scopes_disabled

from pretalx.event.domain.event import initialise_event
from pretalx.event.models import Event, Organiser, Team
from pretalx.person.enums import EmailVerificationState
from pretalx.person.models import User


@pytest.fixture(scope="session", autouse=True)
def collect_static(request):
    management.call_command("collectstatic", "--noinput", "--clear")


@pytest.fixture(autouse=True)
def clear_caches():
    for cache in caches.all():
        cache.clear()


@pytest.fixture
def admin_user():
    return User.objects.create_user(
        password="adminpassw0rd",
        email="admin@example.org",
        name="Admin User",
        is_administrator=True,
        email_verification_state=EmailVerificationState.VERIFIED,
    )


@pytest.fixture
def admin_client(admin_user, client):
    client.force_login(admin_user)
    return client


@pytest.fixture
def organiser():
    with scopes_disabled():
        o = Organiser.objects.create(name="Super Organiser", slug="superorganiser")
        Team.objects.create(
            name="Organisers",
            organiser=o,
            can_create_events=True,
            can_change_teams=True,
            can_change_organiser_settings=True,
            can_change_event_settings=True,
            can_change_submissions=True,
        )
        Team.objects.create(name="Reviewers", organiser=o, is_reviewer=True)
    return o


@pytest.fixture
def event(organiser):
    today = dt.date.today()
    with scopes_disabled():
        event = Event.objects.create(
            name="Fancy testevent",
            is_public=True,
            slug="test",
            email="orga@orga.org",
            date_from=today,
            date_to=today + dt.timedelta(days=3),
            organiser=organiser,
        )
        initialise_event(event)
        for team in organiser.teams.all():
            team.limit_events.add(event)
    return event


@pytest.fixture
def orga_user(event):
    with scopes_disabled():
        user = User.objects.create_user(
            password="orgapassw0rd",
            email="orgauser@orga.org",
            name="Orga User",
            email_verification_state=EmailVerificationState.VERIFIED,
        )
        team = event.organiser.teams.filter(
            can_change_organiser_settings=True, is_reviewer=False
        ).first()
        team.members.add(user)
        team.save()
    return user


@pytest.fixture
def review_user(event):
    with scopes_disabled():
        user = User.objects.create_user(
            password="reviewpassw0rd",
            email="reviewuser@orga.org",
            name="Review User",
            email_verification_state=EmailVerificationState.VERIFIED,
        )
        team = event.organiser.teams.filter(
            can_change_organiser_settings=False, is_reviewer=True
        ).first()
        team.members.add(user)
        team.save()
    return user


@pytest.fixture
def orga_client(orga_user, client):
    client.force_login(orga_user)
    return client


@pytest.fixture
def review_client(review_user, client):
    client.force_login(review_user)
    return client


@pytest.fixture
def settings_url(organiser):
    return reverse(
        "plugins:pretalx_frab_migration:settings", kwargs={"organiser": organiser.slug}
    )


@pytest.fixture
def frab_database(tmp_path):
    path = tmp_path / "frab #1.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "CREATE TABLE conferences (id INTEGER PRIMARY KEY, title TEXT NOT NULL, acronym TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO conferences (id, title, acronym) VALUES (?, ?, ?)",
            [(7, "Zeta Conference", "ZC"), (42, "Alpha & Friends", "AF")],
        )
        connection.commit()
    return path


@pytest.fixture
def migration_database(tmp_path):
    path = tmp_path / "migration.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("""
            CREATE TABLE conferences (
                id INTEGER PRIMARY KEY, title TEXT, acronym TEXT, timezone TEXT,
                start_date DATETIME, end_date DATETIME, email TEXT, color TEXT,
                feedback_enabled BOOLEAN,
                schedule_html_intro TEXT, parent_id INTEGER,
                allowed_event_types TEXT, feedback_token TEXT, program_export_base_url TEXT,
                timeslot_duration INTEGER DEFAULT 15, default_timeslots INTEGER DEFAULT 3
            )
        """)
        connection.execute("""
            CREATE TABLE call_for_participations (
                id INTEGER PRIMARY KEY, conference_id INTEGER, contact_email TEXT,
                start_date DATE, end_date DATE, hard_deadline DATE, welcome_text TEXT, info_url TEXT
            )
        """)
        connection.execute("""
            CREATE TABLE tracks (
                id INTEGER PRIMARY KEY, conference_id INTEGER, name TEXT, color TEXT
            )
        """)
        connection.execute("""
            CREATE TABLE languages (
                id INTEGER PRIMARY KEY, code TEXT, attachable_id INTEGER, attachable_type TEXT
            )
        """)
        connection.execute("""CREATE TABLE people (
            id INTEGER PRIMARY KEY, public_name TEXT, first_name TEXT, last_name TEXT,
            email TEXT, abstract TEXT, description TEXT, note TEXT
        )""")
        connection.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, conference_id INTEGER)"
        )
        connection.execute(
            "CREATE TABLE event_people (id INTEGER PRIMARY KEY, event_id INTEGER, person_id INTEGER, event_role TEXT)"
        )
        connection.execute("""
            INSERT INTO conferences VALUES (
                42, 'Alpha & Friends', 'AF_2026', 'Berlin',
                '2026-10-21 23:30:00', '2026-10-24 21:00:00',
                'orga@example.org', '12AbEF', 0,
                '<h2>Welcome</h2><p>Hello <strong>friends</strong>.</p><p><a href="/info">Details</a></p>',
                NULL, 'lecture;workshop', 'do-not-import-this-token', NULL, 15, 3
            )
        """)
        connection.commit()
    return path
