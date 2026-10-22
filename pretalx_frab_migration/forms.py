from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.utils.translation import gettext_lazy as _
from django_scopes import scopes_disabled

from pretalx.common.forms.widgets import PasswordInput
from pretalx.event.models import Event
from pretalx.event.validators.event import validate_event_slug_unique
from pretalx.orga.forms.admin import GlobalSettingsForm

from .migration import conference_slug, missing_event_fields


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

    def __init__(self, *args, conferences, conference=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["conference"].choices = [("", _("Select a conference"))] + [
            (str(conference_id), f"{title} ({acronym})")
            for conference_id, title, acronym in conferences
        ]
        self.missing_fields = missing_event_fields(conference) if conference else []
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

        self.event_slug = ""
        self.slug_error = None
        if conference:
            value = conference["acronym"]
            if "slug" in self.missing_fields:
                values = self.data if self.is_bound else self.initial
                value = (values.get("slug") or "").strip()
            self.event_slug = conference_slug(value)
            self.check_slug()

    def check_slug(self):
        self.slug_error = None
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
