import hashlib
import json
from collections import Counter
from urllib.parse import quote, urlsplit

from django import forms
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.core.validators import URLValidator
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _

from pretalx.person.enums import SpeakerProfileOrigin
from pretalx.person.models import SpeakerProfile, User
from pretalx.submission.domain.question import save_answer
from pretalx.submission.enums import QuestionRequired, QuestionTarget, QuestionVariant
from pretalx.submission.models import Question

from .import_state import save_import
from .migration import conference_intro

PROFILE_FIELDS = ("name", "email", "biography", "internal_notes")
URL_IDENTIFIER = "frab_migration_source_url"


def check_speaker_support():
    try:
        for field in (
            "email",
            "locale",
            "origin",
            "guid",
            "invitation_token",
            "invitation_sent",
        ):
            SpeakerProfile._meta.get_field(field)
    except FieldDoesNotExist:
        raise ValidationError(
            _("This pretalx version does not support managed speaker imports.")
        ) from None


def validate_source_url(value):
    value = (value or "").strip()
    if not value:
        return ""
    URLValidator(schemes=["http", "https"])(value)
    parts = urlsplit(value)
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValidationError(
            _("The frab base URL must have no credentials, query, or fragment.")
        )
    return value


def person_url(base, acronym, person_id):
    base = validate_source_url(base).rstrip("/")
    if not base:
        return ""
    if not acronym or not str(acronym).strip():
        raise ValidationError(
            _("The frab conference has no acronym for its source URL.")
        )
    result = f"{base}/{quote(str(acronym), safe='')}/people/{int(person_id)}"
    return forms.URLField().clean(result)


def source_values(person, base):
    name = (person["public_name"] or "").strip() or " ".join(
        part.strip()
        for part in (person["first_name"] or "", person["last_name"] or "")
        if part.strip()
    )
    texts = [
        conference_intro(person[field], base) for field in ("abstract", "description")
    ]
    return {
        "name": name,
        "email": (person["email"] or "").strip(),
        "biography": "\n\n".join(dict.fromkeys(text for text in texts if text)),
        "internal_notes": person["note"] or "",
    }


def profile_values(profile):
    return {field: getattr(profile, field) or "" for field in PROFILE_FIELDS}


def matching_user(name, email):
    if not email:
        return None, []
    accounts = list(User.objects.filter(email__iexact=email).order_by("pk"))
    if len(accounts) > 1:
        raise ValidationError(
            _(
                "Several pretalx users have this email address. Correct the accounts before import."
            )
        )
    match = (
        accounts[0]
        if accounts
        and (accounts[0].name or "").strip() == name.strip()
        and name.strip()
        else None
    )
    return match, accounts


def value_hash(value):
    return hashlib.sha256((value or "").encode()).hexdigest()


def field_update(old, current, source, *, has_snapshot):
    if current == source:
        return current, False
    if has_snapshot:
        if value_hash(source) == old:
            return current, False
        if value_hash(current) == old and source:
            return source, False
        return current, True
    if not current:
        return source, False
    return current, True


def snapshot_state(conference, people, mapping, profiles, accounts, source_url):
    data = {
        "conference": conference,
        "people": people,
        "source_url": source_url,
        "mapping": [mapping.event_id, mapping.data] if mapping else None,
        "profiles": [
            [
                p.pk,
                p.code,
                p.user_id,
                p.guid,
                p.origin,
                p.locale,
                p.has_arrived,
                p.profile_picture_id,
                p.invitation_token,
                p.invitation_sent,
                profile_values(p),
                list(
                    p.answers.order_by("pk").values(
                        "pk", "question_id", "answer", "answer_file"
                    )
                ),
                list(p.availabilities.order_by("pk").values()),
                list(p.speaker_roles.order_by("pk").values()),
            ]
            for p in profiles
        ],
        "users": [
            [u.pk, u.name, u.email, u.is_active, u.profile_picture_id]
            for u in sorted(accounts.values(), key=lambda u: u.pk)
        ],
        "questions": list(
            Question.all_objects.filter(event_id=mapping.event_id)
            .order_by("pk")
            .values()
        )
        if mapping and mapping.event_id
        else [],
    }
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str).encode()
    ).hexdigest()


def ensure_url_question(mapping, *, repair=False):
    event = mapping.event
    question = mapping.url_question
    if question:
        valid = (
            question.event_id == event.pk
            and question.identifier == URL_IDENTIFIER
            and question.target == QuestionTarget.SPEAKER
            and question.variant == QuestionVariant.URL
            and not question.is_public
            and not question.is_visible_to_reviewers
            and question.active
            and question.contains_personal_data
            and question.read_only
        )
        if valid:
            return question
        if (
            not repair
            or question.target != QuestionTarget.SPEAKER
            or question.variant != QuestionVariant.URL
            or question.event_id != event.pk
        ):
            raise ValidationError(
                _(
                    "The frab source URL field changed. Review its settings before import."
                )
            )
        question.is_public = False
        question.is_visible_to_reviewers = False
        question.contains_personal_data = True
        question.active = True
        question.freeze_after = now()
        question.save()
        return question
    if mapping.has_url_question and not repair:
        raise ValidationError(
            _(
                "The frab source URL field was deleted. Confirm its repair before import."
            )
        )
    if Question.all_objects.filter(event=event, identifier=URL_IDENTIFIER).exists():
        raise ValidationError(
            _(
                "Another custom field uses the frab source URL identifier. Resolve this conflict first."
            )
        )
    question = Question(
        event=event,
        identifier=URL_IDENTIFIER,
        target=QuestionTarget.SPEAKER,
        variant=QuestionVariant.URL,
        question={event.locale: "frab source URL"},
        question_required=QuestionRequired.OPTIONAL,
        is_public=False,
        is_visible_to_reviewers=False,
        contains_personal_data=True,
        active=True,
        freeze_after=now(),
    )
    question.full_clean(exclude=["icon"])
    question.save()
    mapping.url_question = question
    mapping.has_url_question = True
    save_import(mapping)
    return question


def save_profile(profile, values, user):
    previous = profile_values(profile)
    changed = [field for field in PROFILE_FIELDS if previous[field] != values[field]]
    for field in changed:
        setattr(profile, field, values[field])
    if "email" in changed and profile.invitation_token:
        profile.invitation_token = None
        profile.invitation_sent = None
        changed.extend(["invitation_token", "invitation_sent"])
    profile.full_clean(exclude=["code", "guid"])
    if changed:
        profile.save(update_fields=changed)
        profile.log_action(
            "pretalx.user.profile.update",
            person=user,
            orga=True,
            data={"source": "frab", "old": previous, "new": values},
        )


def write_source_answer(question, profile, value):
    answers = list(profile.answers.filter(question=question).order_by("pk"))
    if len(answers) > 1:
        raise ValidationError(
            _(
                "This speaker has duplicate frab source URL answers. Remove the duplicate before import."
            )
        )
    save_answer(
        question=question,
        value=value,
        target_object=profile,
        existing=answers[0] if answers else None,
    )


def apply_speaker_import(preview, mapping, user):
    from pretalx.person.domain.profile import merge_speaker_profiles  # noqa: PLC0415

    counts = Counter()
    question = (
        ensure_url_question(
            mapping, repair=preview.cleaned_data.get("repair_url_question", False)
        )
        if preview.source_url and preview.groups
        else None
    )
    for group in preview.groups:
        primary = group["primary"]
        profile = group["profile"]
        account = primary["account"]
        if profile is None:
            if account:
                profile = account.get_speaker(
                    mapping.event, origin=SpeakerProfileOrigin.IMPORT
                )
                counts["linked"] += 1
            else:
                profile = SpeakerProfile(
                    event=mapping.event, user=None, origin=SpeakerProfileOrigin.IMPORT
                )
                profile.full_clean(exclude=["code", "guid"])
                profile.save()
                counts["managed"] += 1
            profile.log_action(
                "pretalx.speaker.create",
                person=user,
                orga=True,
                data={"source": "frab"},
            )
        else:
            counts["reused"] += 1
        for merged, choices in group["merges"].values():
            for item in mapping.data["speakers"].values():
                if item["target_code"] == merged.code:
                    item["target_code"] = profile.code
            merge_speaker_profiles(merged, profile, choices=choices, user=user)
            counts["merged"] += 1
        if account and not profile.user_id:
            profile.user = account
            profile.invitation_token = None
            profile.invitation_sent = None
            profile.save(update_fields=["user", "invitation_token", "invitation_sent"])
            profile.log_action(
                "pretalx.user.profile.update",
                person=user,
                orga=True,
                data={"source": "frab", "linked_user": account.pk},
            )
            counts["linked"] += 1
        save_profile(profile, group["values"], user)
        if question:
            write_source_answer(question, profile, group["url"])
        for item in mapping.data["speakers"].values():
            if item["target_code"] == profile.code:
                item["is_primary"] = False
        for entry in group["entries"]:
            snapshot = {field: value_hash(value) for field, value in entry["values"].items()}
            if preview.source_url:
                snapshot["source_url"] = value_hash(entry["url"])
            mapping.data["speakers"][str(entry["id"])] = {
                "target_code": profile.code,
                "snapshot": snapshot,
                "is_primary": entry is primary,
            }
        counts["speakers"] += 1
    counts["skipped"] = sum(entry["skip"] for entry in preview.entries)
    save_import(mapping)
    return counts
