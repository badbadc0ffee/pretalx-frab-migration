import colorsys
import datetime as dt
import re
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bs4 import BeautifulSoup
from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from django_scopes import scope, scopes_disabled
from markdownify import MarkdownConverter

from pretalx.common.ui import _relative_luminance
from pretalx.event.domain.event import create_event, post_create_event
from pretalx.event.models.event import (
    Event,
    default_display_settings,
    default_feature_flags,
)
from pretalx.submission.models import Track

from .timezones import RAILS_TIMEZONES

REQUIRED_EVENT_FIELDS = {
    "name": "title",
    "slug": "acronym",
    "timezone": "timezone",
    "date_from": "start_date",
    "date_to": "end_date",
    "email": "email",
}


def missing_event_fields(conference):
    return [
        field
        for field, source in REQUIRED_EVENT_FIELDS.items()
        if conference[source] is None
        or (isinstance(conference[source], str) and not conference[source].strip())
    ]


def conference_slug(value):
    return (value or "").lower().replace("_", "-")


def conference_timezone(value):
    name = RAILS_TIMEZONES.get(value, value)
    if name == "Etc/UTC":
        name = "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise ValidationError(
            {"timezone": _("The frab timezone is not valid.")}
        ) from None


def conference_date(value, timezone, field):
    try:
        if isinstance(value, str):
            value = dt.datetime.fromisoformat(value)
        if not isinstance(value, dt.datetime):
            raise TypeError  # noqa: TRY301 -- report all invalid dates as field errors
        # Rails stores timestamps without offsets in UTC by default.
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.UTC)
        return value.astimezone(timezone).date()
    except (TypeError, ValueError, OverflowError):
        raise ValidationError(
            {field: _("The frab date is missing or not valid.")}
        ) from None


def conference_boolean(value, field):
    # SQLite returns integers; the server drivers return integers or booleans.
    if value in (False, 0, "0", "f", "false"):
        return False
    if value in (True, 1, "1", "t", "true"):
        return True
    raise ValidationError({field: _("The frab value must be true or false.")})


def conference_intro(value, source_url):
    soup = BeautifulSoup(value or "", "html.parser")
    for element in soup(["script", "style", "iframe", "object", "template"]):
        element.decompose()
    for element in soup.find_all(["a", "img"]):
        attribute = "href" if element.name == "a" else "src"
        target = element.get(attribute)
        if not target:
            continue
        target = urljoin(source_url.rstrip("/") + "/", target) if source_url else target
        try:
            valid = urlsplit(target).scheme.lower() in ("http", "https", "mailto")
        except ValueError:
            valid = False
        if valid:
            element[attribute] = target
        else:
            del element[attribute]
    return MarkdownConverter(heading_style="ATX").convert_soup(soup).strip()


def conference_color(value, field):
    color = (value or "").strip()
    if not color:
        return None
    color = "#" + color.removeprefix("#")
    if not re.fullmatch(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})", color):
        raise ValidationError(
            {field: _("The frab colour must contain three or six hexadecimal digits.")}
        )
    return color


def track_color(value):
    color = conference_color(value, "color") or "#fefd7f"
    digits = color[1:]
    if len(digits) == 3:
        digits = "".join(channel * 2 for channel in digits)
    rgb = tuple(int(digits[index : index + 2], 16) / 255 for index in (0, 2, 4))
    hue, _, value = colorsys.rgb_to_hsv(*rgb)
    # Match colorpicker.js: rounded contrast >= 2.5 on white and #121416.
    dark_luminance = _relative_luminance(18 / 255, 20 / 255, 22 / 255)
    readable = []
    for level in range(256):
        channels = tuple(
            round(channel * 255)
            for channel in colorsys.hsv_to_rgb(hue, 0.8, level / 255)
        )
        luminance = _relative_luminance(*(channel / 255 for channel in channels))
        if (
            round(1.05 / (luminance + 0.05), 1) >= 2.5
            and round((luminance + 0.05) / (dark_luminance + 0.05), 1) >= 2.5
        ):
            readable.append(
                (level, "#" + "".join(f"{channel:02X}" for channel in channels))
            )
    return min(readable, key=lambda candidate: abs(candidate[0] - round(value * 255)))[
        1
    ]


def migrate_tracks(event, tracks):
    for position, source in enumerate(tracks):
        try:
            name = forms.CharField(
                max_length=Track._meta.get_field("name").max_length
            ).clean(source["name"])
            track = Track(
                event=event,
                name={event.locale: name},
                color=track_color(source["color"]),
                position=position,
            )
            track.full_clean()
        except ValidationError as exc:
            raise ValidationError(
                {
                    "tracks": _("Could not migrate frab track {id}: {error}").format(
                        id=source["id"], error=" ".join(exc.messages)
                    )
                }
            ) from exc
        track.save()
    if tracks:
        event.cfp.fields["track"]["visibility"] = "optional"
        event.cfp.save(update_fields=["fields"])


def migrate_cfp(event, source, source_url):
    dates = {}
    for field in ("start_date", "hard_deadline", "end_date"):
        if field == "end_date" and dates["hard_deadline"]:
            continue
        try:
            dates[field] = forms.DateField(required=False).clean(source[field])
        except ValidationError as exc:
            raise ValidationError({f"cfp_{field}": exc.messages}) from exc
    deadline_field = "hard_deadline" if dates["hard_deadline"] else "end_date"
    deadline = dates[deadline_field]
    if dates["start_date"] and deadline and deadline < dates["start_date"]:
        raise ValidationError(
            {
                f"cfp_{deadline_field}": _(
                    "The CfP deadline must not precede its opening date."
                )
            }
        )
    cfp = event.cfp
    cfp.opening = (
        dt.datetime.combine(dates["start_date"], dt.time.min, tzinfo=event.tz)
        if dates["start_date"]
        else None
    )
    cfp.deadline = (
        dt.datetime.combine(deadline, dt.time.max, tzinfo=event.tz)
        if deadline
        else None
    )
    cfp.text = {event.locale: conference_intro(source["welcome_text"], source_url)}
    cfp.full_clean()
    cfp.save(update_fields=["opening", "deadline", "text"])


@transaction.atomic
def migrate_conference(
    conference, *, organiser, user, source_url="", overrides=None, import_tracks=True
):
    """Create the selected event with the native pretalx creation function."""
    missing = missing_event_fields(conference)
    overrides = {
        key: value for key, value in (overrides or {}).items() if key in missing
    }
    timezone = conference_timezone(overrides.get("timezone", conference["timezone"]))
    try:
        name = forms.CharField(
            max_length=Event._meta.get_field("name").max_length
        ).clean(overrides.get("name", conference["title"]))
    except ValidationError as exc:
        raise ValidationError({"name": exc.messages}) from exc
    dates = {
        field: overrides.get(field)
        if field in missing
        else conference_date(conference[REQUIRED_EVENT_FIELDS[field]], timezone, field)
        for field in ("date_from", "date_to")
    }
    color = conference_color(conference["color"], "primary_color")
    flags = default_feature_flags()
    flags["use_tracks"] = import_tracks
    flags["use_feedback"] = conference_boolean(
        conference["feedback_enabled"], "feedback_enabled"
    )
    locale = settings.LANGUAGE_CODE
    with scopes_disabled():
        event = create_event(
            organiser=organiser,
            user=user,
            locales=[locale],
            locale=locale,
            name={locale: name},
            slug=conference_slug(overrides.get("slug", conference["acronym"])),
            timezone=timezone.key,
            date_from=dates["date_from"],
            date_to=dates["date_to"],
            email=(overrides.get("email", conference["email"]) or "").strip(),
            primary_color=color or None,
            feature_flags=flags,
            display_settings={
                **default_display_settings(),
                "html_export_url": conference["program_export_base_url"] or "",
            },
            landing_page_text={
                locale: conference_intro(conference["schedule_html_intro"], source_url)
            },
            is_public=True,
        )
    with scope(event=event):
        post_create_event(event, user=user)
        if conference["cfp"]:
            migrate_cfp(event, conference["cfp"], source_url)
        if import_tracks:
            migrate_tracks(event, conference["tracks"])
    return event
