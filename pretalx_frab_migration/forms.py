import uuid

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from django_scopes import scopes_disabled

from pretalx.common.forms.widgets import PasswordInput
from pretalx.event.models import Event
from pretalx.event.validators.event import validate_event_slug_unique
from pretalx.orga.forms.admin import GlobalSettingsForm

from .migration import (
    conference_slug,
    conference_submission_types,
    missing_event_fields,
)
from .speakers import validate_source_url


class FrabMigrationSettingsForm(GlobalSettingsForm):
    frab_migration_url = forms.URLField(
        label=_("frab URL"),
        help_text=_("Enter the base URL of the frab system. Use HTTP or HTTPS."),
        required=False,
        validators=[URLValidator(schemes=["http", "https"])],
    )
    frab_migration_db_backend = forms.ChoiceField(
        label=_("Database type"),
        choices=[
            ("", _("Not configured")),
            ("sqlite3", "SQLite"),
            ("mysql", "MySQL"),
            ("postgresql", "PostgreSQL"),
        ],
        help_text=_(
            'Select the frab database type. Select "Not configured" to remove '
            "all saved database settings. SQLite removes the saved server access settings."
        ),
        required=False,
    )
    frab_migration_db_name = forms.CharField(
        label=_("Database name or file path"),
        help_text=_(
            "For SQLite, enter the full file path on the pretalx server. "
            "For MySQL or PostgreSQL, enter the database name."
        ),
        required=False,
    )
    frab_migration_db_host = forms.CharField(
        label=_("Database host"),
        help_text=_(
            "For MySQL or PostgreSQL. Enter a host name, IP address, or socket path. "
            "Leave this field empty to use the local server. SQLite does not use this field."
        ),
        required=False,
    )
    frab_migration_db_port = forms.IntegerField(
        label=_("Database port"),
        help_text=_(
            "Leave this field empty to use the default port (MySQL: 3306; PostgreSQL: 5432). "
            "SQLite does not use this field."
        ),
        min_value=1,
        max_value=65535,
        required=False,
    )
    frab_migration_db_user = forms.CharField(
        label=_("Database user"),
        help_text=_(
            "For MySQL or PostgreSQL. Leave this field empty to use the database client's default user. "
            "SQLite does not use this field."
        ),
        required=False,
    )
    frab_migration_db_password = forms.CharField(
        label=_("Database password"),
        help_text=_(
            "Leave this field empty to keep the saved password. "
            "SQLite does not use this field."
        ),
        widget=PasswordInput(attrs={"autocomplete": "new-password"}),
        strip=False,
        required=False,
    )

    frab_migration_new_source = forms.BooleanField(
        label=_("Use a different frab database"),
        required=False,
        help_text=_(
            "Select this only when you switch to another source database. Keep it clear for password, server, or URL changes."
        ),
    )

    def clean_frab_migration_url(self):
        return validate_source_url(self.cleaned_data["frab_migration_url"])

    @transaction.atomic
    def save(self):
        new_source = self.cleaned_data["frab_migration_new_source"]
        self.cleaned_data["frab_migration_new_source"] = False
        super().save()
        if self.cleaned_data["frab_migration_db_backend"] and (
            new_source
            or not self.obj.settings.get("frab_migration_source_id", default="")
        ):
            self.obj.settings.set("frab_migration_source_id", str(uuid.uuid4()))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.pop("frab_migration_db_password", None)

    def clean(self):
        cleaned_data = super().clean()
        backend = cleaned_data.get("frab_migration_db_backend")
        if backend and not cleaned_data.get("frab_migration_db_name"):
            self.add_error(
                "frab_migration_db_name",
                _("Enter the SQLite file path or the database name."),
            )

        if not backend:
            cleaned_data["frab_migration_db_name"] = ""
        if backend in (None, "", "sqlite3"):
            for field in ("host", "user", "password"):
                cleaned_data[f"frab_migration_db_{field}"] = ""
            cleaned_data["frab_migration_db_port"] = None
        elif not cleaned_data.get("frab_migration_db_password"):
            cleaned_data["frab_migration_db_password"] = self.obj.settings.get(
                "frab_migration_db_password", default=""
            )
        return cleaned_data


class FrabConferenceForm(forms.Form):
    connection = forms.CharField(widget=forms.HiddenInput)
    conference = forms.ChoiceField(label=_("Conference"))

    def __init__(
        self,
        *args,
        conferences,
        conference=None,
        event=None,
        existing_events=(),
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.fields["conference"].choices = [("", _("Select a conference"))] + [
            (str(conference_id), f"{title} ({acronym})")
            for conference_id, title, acronym in conferences
        ]
        self.target_event = event
        if existing_events:
            self.fields["existing_event"] = forms.ChoiceField(
                label=_("Previously imported event"),
                required=False,
                choices=[("", _("Create a new event"))]
                + [(str(e.pk), str(e.name)) for e in existing_events],
                help_text=_(
                    "Select an event only to link an earlier import to this source conference. This action imports speakers only."
                ),
            )
        self.missing_fields = (
            missing_event_fields(conference) if conference and not event else []
        )
        for name in self.missing_fields:
            model_field = Event._meta.get_field(name)
            if name == "name":
                field = forms.CharField(max_length=model_field.max_length)
            elif name in ("date_from", "date_to"):
                field = forms.DateField(
                    widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
                )
            else:
                field = model_field.formfield()
            field.label = model_field.verbose_name
            field.help_text = _(
                "frab has no value for this field. Enter a value for the new event."
            )
            field.required = True
            self.fields[name] = field

        if conference and not event and len(conference["tracks"]) > 1:
            self.fields["import_tracks"] = forms.BooleanField(
                label=_("Migrate tracks"),
                required=False,
                initial=True,
                help_text=_(
                    'Clear this checkbox to skip all tracks and disable "Use tracks" for the new event.'
                ),
            )

        if (
            conference
            and not event
            and len(conference_submission_types(conference)) > 1
        ):
            self.fields["import_submission_types"] = forms.BooleanField(
                label=_("Migrate session types"),
                required=False,
                initial=True,
                help_text=_(
                    'Clear this checkbox to keep only the default "Talk" session type and its default duration.'
                ),
            )

        self.event_slug = ""
        self.slug_error = None
        if conference and not event:
            value = conference["acronym"]
            if "slug" in self.missing_fields:
                values = self.data if self.is_bound else self.initial
                value = (values.get("slug") or "").strip()
            self.event_slug = conference_slug(value)
            self.check_slug()

    def check_slug(self):
        self.slug_error = None
        if self.target_event:
            return None
        with scopes_disabled():
            try:
                validate_event_slug_unique(self.event_slug)
            except ValidationError:
                self.slug_error = _(
                    "This short name is already taken. Please choose another one."
                )
        return self.slug_error

    def clean(self):
        cleaned_data = super().clean()
        if self.slug_error:
            self.add_error("slug" if "slug" in self.fields else None, self.slug_error)
        return cleaned_data
