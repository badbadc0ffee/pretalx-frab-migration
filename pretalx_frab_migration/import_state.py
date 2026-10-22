import json
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from pretalx.common.models.settings import GlobalSettings_SettingsStore
from pretalx.event.models import Event
from pretalx.person.models import SpeakerProfile
from pretalx.submission.models import Question

IMPORT_KEY = "frab_migration"
SOURCE_KEY = "frab_migration_source_conference"


def load_import(event, *, source_id=None, conference_id=None):
    # Read the native settings row directly so transaction rollbacks cannot leave a stale snapshot.
    raw = (
        event._settings_objects.filter(key=IMPORT_KEY)  # noqa: SLF001 -- native Hierarkey relation; read without cache
        .values_list("value", flat=True)
        .first()
    )
    data = (
        json.loads(raw)
        if raw
        else {
            "source_id": str(source_id),
            "conference_id": conference_id,
            "organiser_id": event.organiser_id,
            "speakers": {},
        }
    )
    profiles = dict(
        SpeakerProfile.objects.filter(event=event).values_list("code", "pk")
    )
    speakers = [
        SimpleNamespace(
            person_id=int(pid), profile_id=profiles.get(item["target_code"]), **item
        )
        for pid, item in data.get("speakers", {}).items()
    ]
    question_id = data.get("url_question_id")
    question = (
        Question.all_objects.filter(event=event, pk=question_id).first()
        if question_id
        else None
    )
    return SimpleNamespace(
        pk=event.pk if raw else None,
        event=event,
        event_id=event.pk,
        data=data,
        source_id=data["source_id"],
        conference_id=data["conference_id"],
        organiser_id=data["organiser_id"],
        speakers=speakers,
        url_question=question,
        url_question_id=question_id,
        has_url_question=bool(question_id),
    )


def save_import(state):
    state.data["url_question_id"] = (
        state.url_question.pk if state.url_question else state.url_question_id
    )
    state.event.settings.set(IMPORT_KEY, state.data)
    state.event.settings.set(SOURCE_KEY, f"{state.source_id}:{state.conference_id}")
    state.pk = state.event.pk


def find_import(source_id, conference_id, organiser):
    events = list(
        Event.objects.filter(
            _settings_objects__key=SOURCE_KEY,
            _settings_objects__value=f"{source_id}:{conference_id}",
        )
        .select_related("cfp")
        .distinct()
    )
    if len(events) > 1:
        raise ValidationError(
            _(
                "Several events refer to this frab conference. Correct their import settings first."
            )
        )
    if not events:
        return None
    state = load_import(events[0])
    if state.organiser_id != organiser.pk or state.event.organiser_id != organiser.pk:
        raise ValidationError(
            _("This source conference belongs to another organiser in pretalx.")
        )
    return state


def lock_source(source_id):
    # Lock the existing global setting until the surrounding transaction ends.
    # UPDATE also serialises imports on SQLite, where SELECT FOR UPDATE is a no-op.
    updated = GlobalSettings_SettingsStore.objects.filter(
        key="frab_migration_source_id", value=str(source_id)
    ).update(value=str(source_id))
    if not updated:
        raise ValidationError(
            _("The source settings changed. Connect again before importing.")
        )
