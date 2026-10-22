import sqlite3
from contextlib import closing
from unittest.mock import Mock

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django_scopes import scopes_disabled

from pretalx.common.models.settings import GlobalSettings
from pretalx.event.models import Event, Organiser
from pretalx.mail.models import QueuedMail
from pretalx.person.domain.profile import claim_speaker_profile, merge_speaker_profiles
from pretalx.person.models import SpeakerProfile, User
from pretalx.submission.domain.queries.question import (
    public_answers_for_speaker,
    questions_for_user,
)
from pretalx.submission.interfaces.forms import QuestionsForm
from pretalx.submission.models import Answer, Question, SpeakerRole, Submission

from pretalx_frab_migration import database, views
from pretalx_frab_migration.database import fetch_speakers
from pretalx_frab_migration.import_state import SOURCE_KEY, load_import
from pretalx_frab_migration.speakers import field_update, person_url, value_hash

pytestmark = pytest.mark.django_db


def get_import(person_id=None):
    with scopes_disabled():
        event = (
            Event.objects.filter(_settings_objects__key=SOURCE_KEY)
            .select_related("cfp")
            .get()
        )
        state = load_import(event)
        if person_id is None:
            return state
        entry = next(item for item in state.speakers if item.person_id == person_id)
        entry.profile = (
            SpeakerProfile.objects.filter(event=event, code=entry.target_code)
            .select_related("event__cfp", "profile_picture", "user")
            .first()
        )
        return entry


def edit_person(path, person_id=1, **fields):
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            f"UPDATE people SET {', '.join(f'{key} = ?' for key in fields)} WHERE id = ?",
            (*fields.values(), person_id),
        )
        connection.commit()


def post_data(response):
    result = {
        "action": "migrate",
        "speakers_preview": response.context["speakers_preview"],
    }
    for key in ("conference_form", "speaker_form"):
        form = response.context[key]
        for name in form.fields:
            value = form[name].value()
            if value is True:
                value = "on"
            result[name] = value if value not in (None, False) else ""
    return result


@pytest.fixture
def people_db(migration_database):
    with closing(sqlite3.connect(migration_database)) as connection:
        connection.executemany(
            "INSERT INTO people VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    1,
                    "Alice Example",
                    "Alice",
                    "Example",
                    "alice@example.org",
                    "<p>About Alice</p>",
                    "<p>More detail</p>",
                    "Private note",
                ),
                (2, "Moderator", "", "", "", "", "", ""),
                (3, "Submitter", "", "", "submitter@example.org", "", "", ""),
                (4, "Elsewhere", "", "", "other@example.org", "", "", ""),
            ],
        )
        connection.executemany(
            "INSERT INTO events VALUES (?, ?)", [(10, 42), (11, 42), (12, 99)]
        )
        connection.executemany(
            "INSERT INTO event_people VALUES (?, ?, ?, ?)",
            [
                (1, 10, 1, "speaker"),
                (2, 11, 1, "moderator"),
                (3, 10, 2, "moderator"),
                (4, 10, 3, "submitter"),
                (5, 12, 4, "speaker"),
            ],
        )
        connection.commit()
    return migration_database


@pytest.fixture
def select_speakers(admin_client, settings_url, people_db):
    def select(**extra):
        response = admin_client.post(
            settings_url,
            {
                "action": "connect",
                "frab_migration_db_backend": "sqlite3",
                "frab_migration_db_name": str(people_db),
                "frab_migration_url": extra.pop(
                    "base_url", "https://frab.example.org/frab/"
                ),
            },
        )
        token = response.context["conference_form"].initial["connection"]
        return admin_client.post(
            settings_url,
            {"action": "select", "conference": "42", "connection": token, **extra},
        )

    return select


def test_source_speakers_are_distinct_and_conference_scoped(people_db):
    before = people_db.read_bytes()
    people = fetch_speakers(42, backend="sqlite3", name=str(people_db))
    assert [p["id"] for p in people] == [1, 2]
    assert people_db.read_bytes() == before


@pytest.mark.parametrize("backend", ("mysql", "postgresql"))
def test_source_speaker_query_uses_driver_parameters(backend, monkeypatch):
    driver = database.pymysql if backend == "mysql" else database.psycopg
    connection = Mock()
    cursor = connection.cursor.return_value
    cursor.fetchall.return_value = [(1, "Alice", "", "", "", "", "", "")]
    monkeypatch.setattr(driver, "connect", Mock(return_value=connection))
    assert fetch_speakers(42, backend=backend, name="frab")[0]["public_name"] == "Alice"
    query, params = cursor.execute.call_args.args
    assert params == (42, "speaker", "moderator")
    assert query.count("%s") == 3
    assert "SELECT DISTINCT p.id" in query
    assert "users" not in query
    cursor.close.assert_called_once()
    connection.close.assert_called_once()


def test_managed_speakers_private_links_and_native_defaults(
    select_speakers, admin_client, settings_url, admin_user
):
    response = select_speakers()
    users = User.objects.count()
    assert [e["action"] for e in response.context["speaker_form"].entries] == [
        "Create managed speaker"
    ] * 2
    assert admin_client.post(settings_url, post_data(response)).status_code == 302
    with scopes_disabled():
        mapping = get_import()
        profiles = list(
            SpeakerProfile.objects.filter(event=mapping.event)
            .select_related("event")
            .order_by("pk")
        )
        alice = profiles[0]
        assert alice.user_id is None
        assert alice.origin == "import"
        assert alice.guid
        assert alice.code
        assert alice.biography == "About Alice\n\nMore detail"
        assert alice.internal_notes == "Private note"
        assert not alice.has_arrived
        assert not alice.locale
        question = mapping.url_question
        assert question.target == "speaker"
        assert question.variant == "url"
        assert question.read_only
        assert not question.is_public
        assert not question.is_visible_to_reviewers
        assert question.contains_personal_data
        assert (
            alice.answers.get(question=question).answer
            == "https://frab.example.org/frab/AF_2026/people/1"
        )
        assert question in questions_for_user(mapping.event, admin_user)
        assert (
            not questions_for_user(mapping.event, AnonymousUser())
            .filter(pk=question.pk)
            .exists()
        )
        assert not public_answers_for_speaker(alice).exists()
        form = QuestionsForm(event=mapping.event, speaker=alice, target="speaker")
        assert form.fields[f"question_{question.pk}"].disabled
        assert len(get_import().speakers) == 2
        assert not QueuedMail.objects.exists()
    assert User.objects.count() == users


@pytest.mark.parametrize(
    ("name", "email", "linked"),
    (
        ("Alice Example", "alice@example.org", True),
        (" Alice Example ", "ALICE@example.org", True),
        ("alice example", "alice@example.org", False),
        ("Other Name", "alice@example.org", False),
        ("", "alice@example.org", False),
        ("Alice Example", "different@example.org", False),
    ),
)
def test_user_link_requires_email_and_exact_name(
    name, email, linked, select_speakers, admin_client, settings_url
):
    user = User.objects.create_user(email=email, name=name, is_active=False)
    before = User.objects.filter(pk=user.pk).values().get()
    response = select_speakers()
    assert admin_client.post(settings_url, post_data(response)).status_code == 302
    with scopes_disabled():
        profile = get_import(person_id=1).profile
        assert profile.user_id == (user.pk if linked else None)
    assert User.objects.filter(pk=user.pk).values().get() == before


def test_repeat_import_updates_source_changes_without_duplicates(
    select_speakers, admin_client, settings_url, people_db
):
    assert (
        admin_client.post(settings_url, post_data(select_speakers())).status_code == 302
    )
    edit_person(people_db, description="New description")
    response = select_speakers()
    assert response.context["conference_form"].target_event
    assert not response.context["conference_form"].slug_error
    assert admin_client.post(settings_url, post_data(response)).status_code == 302
    with scopes_disabled():
        assert Event.objects.count() == 1
        assert SpeakerProfile.objects.count() == 2
        assert "New description" in get_import(person_id=1).profile.biography
        assert Question.all_objects.count() == 1


def test_existing_profile_field_conflict_requires_choice(
    event, select_speakers, admin_client, settings_url
):
    user = User.objects.create_user(email="alice@example.org", name="Alice Example")
    with scopes_disabled():
        profile = user.get_speaker(event)
        profile.biography = "Existing biography"
        profile.save()
    response = select_speakers(existing_event=str(event.pk))
    data = post_data(response)
    assert "value_1_biography" in data
    assert admin_client.post(settings_url, data).status_code == 200
    data["value_1_biography"] = "target"
    assert admin_client.post(settings_url, data).status_code == 302
    with scopes_disabled():
        profile.refresh_from_db()
        assert profile.biography == "Existing biography"
        assert get_import(person_id=1).profile == profile
        assert Event.objects.count() == 1


def test_preserve_target_edit_and_review_simultaneous_edit(
    select_speakers, admin_client, settings_url, people_db
):
    assert (
        admin_client.post(settings_url, post_data(select_speakers())).status_code == 302
    )
    with scopes_disabled():
        profile = get_import(person_id=1).profile
        profile.biography = "Speaker edit"
        profile.save()
    response = select_speakers()
    assert "value_1_biography" not in response.context["speaker_form"].fields
    assert admin_client.post(settings_url, post_data(response)).status_code == 302
    edit_person(people_db, description="Source edit")
    response = select_speakers()
    data = post_data(response)
    assert "value_1_biography" in data
    data["value_1_biography"] = "source"
    assert admin_client.post(settings_url, data).status_code == 302
    with scopes_disabled():
        profile.refresh_from_db()
        assert "Source edit" in profile.biography


def test_shared_email_aliases_require_review(
    people_db, select_speakers, admin_client, settings_url
):
    edit_person(people_db, person_id=2, email="alice@example.org")
    response = select_speakers()
    data = post_data(response)
    data.update(target_1="new", target_2="source:1")
    reviewed = admin_client.post(settings_url, {**data, "action": "review"})
    data = post_data(reviewed)
    assert "primary_1" in data
    data["primary_1"] = "1"
    assert admin_client.post(settings_url, data).status_code == 302
    with scopes_disabled():
        first = get_import(person_id=1)
        second = get_import(person_id=2)
        assert first.profile_id == second.profile_id
        assert first.is_primary
        assert not second.is_primary
        assert first.profile.answers.get().answer.endswith("/people/1")


def test_shared_email_cannot_link_a_different_name(
    people_db, select_speakers, admin_client, settings_url
):
    user = User.objects.create_user(email="alice@example.org", name="Alice Example")
    edit_person(people_db, person_id=2, email="alice@example.org")
    response = select_speakers()
    data = post_data(response)
    data["target_2"] = "new"
    assert admin_client.post(settings_url, data).status_code == 302
    with scopes_disabled():
        assert get_import(person_id=1).profile.user_id == user.pk
        assert get_import(person_id=2).profile.user_id is None


def test_missing_name_and_invalid_email_use_correction_fields(
    people_db, select_speakers, admin_client, settings_url
):
    edit_person(
        people_db, public_name="", first_name="", last_name="", email="bad-email"
    )
    response = select_speakers()
    data = post_data(response)
    assert "person_1_name" in data
    assert "person_1_email" in data
    data.update(person_1_name="Corrected Name", person_1_email="")
    response = admin_client.post(settings_url, {**data, "action": "review"})
    assert admin_client.post(settings_url, post_data(response)).status_code == 302
    with scopes_disabled():
        profile = get_import(person_id=1).profile
        assert profile.name == "Corrected Name"
        assert not profile.email
        assert not profile.user_id


def test_stale_source_preview_does_not_create_event(
    people_db, select_speakers, admin_client, settings_url
):
    response = select_speakers()
    edit_person(people_db, description="Changed after preview")
    result = admin_client.post(settings_url, post_data(response))
    assert result.status_code == 200
    assert b"preview is new or has changed" in result.content
    with scopes_disabled():
        assert not Event.objects.exists()
        assert not Event.objects.filter(_settings_objects__key=SOURCE_KEY).exists()


def test_source_change_inside_commit_rolls_back(
    select_speakers, admin_client, settings_url, monkeypatch
):
    response = select_speakers()
    original = views.fetch_speakers
    calls = []

    def read(*args, **kwargs):
        rows = original(*args, **kwargs)
        calls.append(True)
        if len(calls) > 1:
            rows[0]["note"] = "Changed during import"
        return rows

    monkeypatch.setattr(views, "fetch_speakers", read)
    result = admin_client.post(settings_url, post_data(response))
    assert result.status_code == 200
    assert b"source or speaker data changed" in result.content
    with scopes_disabled():
        assert not Event.objects.exists()


def test_later_account_links_managed_profile_without_changing_code(
    select_speakers, admin_client, settings_url
):
    assert (
        admin_client.post(settings_url, post_data(select_speakers())).status_code == 302
    )
    with scopes_disabled():
        profile = get_import(person_id=1).profile
        code, guid = profile.code, profile.guid
    user = User.objects.create_user(email="alice@example.org", name="Alice Example")
    assert (
        admin_client.post(settings_url, post_data(select_speakers())).status_code == 302
    )
    with scopes_disabled():
        profile.refresh_from_db()
        assert profile.user_id == user.pk
        assert (profile.code, profile.guid) == (code, guid)


def test_changed_linked_name_requires_explicit_skip(
    select_speakers, admin_client, settings_url, people_db
):
    User.objects.create_user(email="alice@example.org", name="Alice Example")
    assert (
        admin_client.post(settings_url, post_data(select_speakers())).status_code == 302
    )
    edit_person(people_db, public_name="Different Name")
    response = select_speakers()
    data = post_data(response)
    assert "skip_1" in data
    data["skip_1"] = "on"
    assert admin_client.post(settings_url, data).status_code == 302
    with scopes_disabled():
        assert get_import(person_id=1).profile.name == "Alice Example"


def test_no_base_url_skips_question_and_later_adds_link(
    select_speakers, admin_client, settings_url
):
    response = select_speakers(base_url="")
    assert admin_client.post(settings_url, post_data(response)).status_code == 302
    with scopes_disabled():
        assert not Question.all_objects.exists()
    assert (
        admin_client.post(settings_url, post_data(select_speakers())).status_code == 302
    )
    with scopes_disabled():
        assert Question.all_objects.count() == 1


@pytest.mark.parametrize(
    ("base", "acronym", "expected"),
    (
        ("https://frab.example.org", "CONF", "https://frab.example.org/CONF/people/3"),
        (
            "https://frab.example.org/prefix///",
            "A B/2020",
            "https://frab.example.org/prefix/A%20B%2F2020/people/3",
        ),
    ),
)
def test_source_url_preserves_installation_path(base, acronym, expected):
    assert person_url(base, acronym, 3) == expected


@pytest.mark.parametrize(
    "base",
    (
        "ftp://frab.example.org",
        "https://u:p@frab.example.org",
        "https://frab.example.org/?secret=a",
        "https://frab.example.org/#part",
    ),
)
def test_source_url_rejects_invalid_components(base):
    with pytest.raises(ValidationError):
        person_url(base, "CONF", 3)


@pytest.mark.parametrize(
    ("old", "current", "source", "snapshot", "result", "conflict"),
    (
        ("a", "a", "b", True, "b", False),
        ("a", "edit", "a", True, "edit", False),
        ("a", "", "a", True, "", False),
        ("a", "", "b", True, "", True),
        ("a", "edit", "b", True, "edit", True),
        ("a", "a", "", True, "a", True),
        ("", "existing", "source", False, "existing", True),
    ),
)
def test_three_way_field_rules(old, current, source, snapshot, result, conflict):
    assert field_update(value_hash(old), current, source, has_snapshot=snapshot) == (
        result,
        conflict,
    )


def test_reviewed_merge_moves_talks_answers_and_mapping(
    select_speakers, admin_client, settings_url
):
    assert (
        admin_client.post(settings_url, post_data(select_speakers())).status_code == 302
    )
    user = User.objects.create_user(email="alice@example.org", name="Alice Example")
    with scopes_disabled():
        imported = get_import(person_id=1)
        managed = imported.profile
        event = managed.event
        target = user.get_speaker(event)
        target.internal_notes = "Existing private note"
        target.save()
        talk = Submission.objects.create(
            event=event, title="Existing talk", submission_type_id=event.cfp.default_type_id
        )
        SpeakerRole.objects.create(submission=talk, speaker=managed)
        old_pk = managed.pk
        target_pk = target.pk
    before = User.objects.filter(pk=user.pk).values().get()
    response = select_speakers()
    data = post_data(response)
    data[f"merge_{old_pk}"] = "on"
    assert admin_client.post(settings_url, data).status_code == 302
    with scopes_disabled():
        target.refresh_from_db()
        assert not SpeakerProfile.objects.filter(pk=old_pk).exists()
        assert get_import(person_id=1).profile_id == target_pk
        assert list(talk.speakers.values_list("pk", flat=True)) == [target_pk]
        assert "Existing private note" in target.internal_notes
        assert "Private note" in target.internal_notes
        assert target.answers.get().answer.endswith("/people/1")
    assert User.objects.filter(pk=user.pk).values().get() == before


def test_native_claim_does_not_create_another_profile(
    select_speakers, admin_client, settings_url
):
    assert (
        admin_client.post(settings_url, post_data(select_speakers())).status_code == 302
    )
    user = User.objects.create_user(email="alice@example.org", name="Alice Example")
    with scopes_disabled():
        profile = get_import(person_id=1).profile
        claim_speaker_profile(profile, user)
    assert (
        admin_client.post(settings_url, post_data(select_speakers())).status_code == 302
    )
    with scopes_disabled():
        assert get_import(person_id=1).profile_id == profile.pk
        assert SpeakerProfile.objects.count() == 2


def test_native_merge_requires_mapping_repair(
    select_speakers, admin_client, settings_url
):
    assert (
        admin_client.post(settings_url, post_data(select_speakers())).status_code == 302
    )
    user = User.objects.create_user(email="alice@example.org", name="Alice Example")
    with scopes_disabled():
        imported = get_import(person_id=1)
        managed = imported.profile
        survivor = user.get_speaker(managed.event)
        merge_speaker_profiles(managed, survivor, choices={}, user=user)
        imported = get_import(person_id=1)
        assert imported.profile_id is None
        assert imported.target_code
    response = select_speakers()
    data = post_data(response)
    assert "target_1" in data
    data["target_1"] = f"profile:{survivor.pk}"
    assert admin_client.post(settings_url, data).status_code == 302
    with scopes_disabled():
        assert get_import(person_id=1).profile_id == survivor.pk


def test_stale_target_preview_keeps_profile_data(
    select_speakers, admin_client, settings_url
):
    assert (
        admin_client.post(settings_url, post_data(select_speakers())).status_code == 302
    )
    response = select_speakers()
    with scopes_disabled():
        profile = get_import(person_id=1).profile
        profile.internal_notes = "Later organiser edit"
        profile.save()
    result = admin_client.post(settings_url, post_data(response))
    assert result.status_code == 200
    with scopes_disabled():
        profile.refresh_from_db()
        assert profile.internal_notes == "Later organiser edit"


def test_user_created_after_preview_requires_new_review(
    select_speakers, admin_client, settings_url
):
    response = select_speakers()
    User.objects.create_user(email="alice@example.org", name="Alice Example")
    result = admin_client.post(settings_url, post_data(response))
    assert result.status_code == 200
    with scopes_disabled():
        assert not SpeakerProfile.objects.exists()


def test_private_question_needs_repair_after_public_toggle(
    select_speakers, admin_client, settings_url
):
    assert (
        admin_client.post(settings_url, post_data(select_speakers())).status_code == 302
    )
    with scopes_disabled():
        question = get_import().url_question
        question.is_public = True
        question.save()
    response = select_speakers()
    data = post_data(response)
    assert "repair_url_question" in data
    assert admin_client.post(settings_url, data).status_code == 200
    data["repair_url_question"] = "on"
    assert admin_client.post(settings_url, data).status_code == 302
    with scopes_disabled():
        question.refresh_from_db()
        assert not question.is_public


def test_edited_source_url_requires_choice(select_speakers, admin_client, settings_url):
    assert (
        admin_client.post(settings_url, post_data(select_speakers())).status_code == 302
    )
    with scopes_disabled():
        profile = get_import(person_id=1).profile
        profile.answers.update(answer="https://archive.example.org/old-person")
    response = select_speakers(base_url="https://new-frab.example.org")
    data = post_data(response)
    assert "url_1" in data
    data["url_1"] = "target"
    assert admin_client.post(settings_url, data).status_code == 302
    with scopes_disabled():
        assert profile.answers.get().answer == "https://archive.example.org/old-person"


def test_duplicate_source_answers_stop_the_import(
    select_speakers, admin_client, settings_url
):
    assert (
        admin_client.post(settings_url, post_data(select_speakers())).status_code == 302
    )
    with scopes_disabled():
        mapping = get_import()
        profile = get_import(person_id=1).profile
        Answer.objects.create(
            question=mapping.url_question,
            speaker=profile,
            answer="https://other.example.org",
        )
    response = select_speakers()
    result = admin_client.post(settings_url, post_data(response))
    assert result.status_code == 200
    assert b"duplicate frab source URL" in result.content


def test_reviewers_cannot_read_source_answers(
    select_speakers, admin_client, settings_url, event, review_user
):
    response = select_speakers(existing_event=str(event.pk))
    assert admin_client.post(settings_url, post_data(response)).status_code == 302
    with scopes_disabled():
        question = get_import().url_question
        assert (
            not questions_for_user(event, review_user).filter(pk=question.pk).exists()
        )
        profile = get_import(person_id=1).profile
        form = QuestionsForm(
            event=event, speaker=profile, target="speaker", for_reviewers=True
        )
        assert f"question_{question.pk}" not in form.fields


def test_wrong_organiser_cannot_reuse_mapping(
    select_speakers, admin_client, settings_url
):
    assert (
        admin_client.post(settings_url, post_data(select_speakers())).status_code == 302
    )
    with scopes_disabled():
        other = Organiser.objects.create(name="Other organiser", slug="other")
        Event.objects.update(organiser=other)
    response = select_speakers()
    assert b"another organiser" in response.content
    assert response.context["speaker_form"] is None


def test_source_identity_only_changes_on_explicit_request(
    select_speakers, admin_client, settings_url, people_db
):
    select_speakers()
    first = GlobalSettings().settings.get("frab_migration_source_id")
    select_speakers(base_url="https://changed.example.org/frab")
    assert GlobalSettings().settings.get("frab_migration_source_id") == first
    admin_client.post(
        settings_url,
        {
            "action": "connect",
            "frab_migration_db_backend": "sqlite3",
            "frab_migration_db_name": str(people_db),
            "frab_migration_new_source": "on",
        },
    )
    assert GlobalSettings().settings.get("frab_migration_source_id") != first


def test_raw_html_in_speaker_data_is_escaped(people_db, select_speakers):
    edit_person(
        people_db,
        public_name="<script>alert(1)</script>",
        note="<script>alert(2)</script>",
    )
    response = select_speakers()
    assert b"<script>alert(" not in response.content
    assert b"&lt;script&gt;alert(2)" in response.content


def test_plugin_adds_no_database_models():
    from django.apps import apps

    assert list(apps.get_app_config("pretalx_frab_migration").get_models()) == []


@pytest.mark.django_db(transaction=True)
def test_concurrent_imports_do_not_duplicate_speakers(select_speakers, settings_url, admin_user):
    from concurrent.futures import ThreadPoolExecutor

    from django.db import close_old_connections, connections
    from django.test import Client

    data = post_data(select_speakers())
    clients = [Client(), Client()]
    for client in clients:
        client.force_login(admin_user)

    def run(client):
        close_old_connections()
        try:
            return client.post(settings_url, data).status_code
        finally:
            connection = connections["default"]
            if connection.connection:
                connection.connection.close()
                connection.connection = None

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(run, clients))
    assert set(statuses) <= {200, 302}
    assert statuses.count(302) <= 1
    with scopes_disabled():
        exists = Event.objects.filter(_settings_objects__key=SOURCE_KEY).exists()
    if not exists:
        assert clients[0].post(settings_url, data).status_code == 302
    with scopes_disabled():
        assert Event.objects.count() == 1
        assert SpeakerProfile.objects.count() == 2
