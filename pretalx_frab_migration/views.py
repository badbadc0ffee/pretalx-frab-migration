import json

from django.contrib import messages
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.shortcuts import redirect
from django.utils.crypto import salted_hmac
from django.utils.translation import gettext_lazy as _
from django.views.generic import FormView

from pretalx.common.text.phrases import phrases
from pretalx.common.ui import Button
from pretalx.common.views.mixins import PermissionRequired

from .database import FrabDatabaseError, fetch_conference, fetch_conferences
from .forms import FrabConferenceForm, FrabMigrationSettingsForm
from .migration import migrate_conference, missing_event_fields

CONNECTION_SALT = "pretalx_frab_migration.connection"
CONNECTION_MAX_AGE = 3600
DATABASE_KEYS = ("backend", "name", "host", "port", "user", "password")


class FrabMigrationView(PermissionRequired, FormView):
    permission_required = "person.administrator_user"
    template_name = "pretalx_frab_migration/settings.html"
    form_class = FrabMigrationSettingsForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["submit_buttons"] = [
            Button(name="action", value="save"),
            Button(name="action", value="connect", label=_("Connect"), color="primary"),
        ]
        return context

    def get_success_url(self):
        return self.request.path

    def post(self, request, *args, **kwargs):
        if request.POST.get("action") in ("select", "check_slug", "migrate"):
            return self.migrate()
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        if self.request.POST.get("action") == "connect":
            return self.connect(form)
        form.save()
        messages.success(self.request, phrases.base.saved)
        return super().form_valid(form)

    def connection_identity(self, options, source_url):
        # Bind the selection to its source, user, and organiser. Never send credentials.
        fingerprint = salted_hmac(
            CONNECTION_SALT,
            json.dumps({**options, "url": source_url}, sort_keys=True),
            algorithm="sha256",
        ).hexdigest()
        return [self.request.user.pk, self.request.organiser.pk, fingerprint]

    def connect(self, form):
        options = {
            key: form.cleaned_data[f"frab_migration_db_{key}"] for key in DATABASE_KEYS
        }
        try:
            conferences = fetch_conferences(**options)
        except FrabDatabaseError as exc:
            messages.error(self.request, str(exc))
            return self.render_to_response(self.get_context_data(form=form))
        form.save()
        token = signing.dumps(
            self.connection_identity(options, form.cleaned_data["frab_migration_url"]),
            salt=CONNECTION_SALT,
        )
        conference_form = (
            FrabConferenceForm(conferences=conferences, initial={"connection": token})
            if conferences
            else None
        )
        messages.success(
            self.request,
            _("Connection successful. Select a conference.")
            if conferences
            else _("Connection successful. This database has no conferences."),
        )
        return self.render_to_response(
            self.get_context_data(
                form=form, connected=True, conference_form=conference_form
            )
        )

    def migrate(self):
        # A conference-only POST must never bind or save the global settings form.
        form = self.form_class()
        options = {
            key: form.obj.settings.get(f"frab_migration_db_{key}", default="") or ""
            for key in DATABASE_KEYS
        }
        options["port"] = options["port"] or None
        source_url = form.obj.settings.get("frab_migration_url", default="") or ""
        try:
            identity = signing.loads(
                self.request.POST.get("connection", ""),
                salt=CONNECTION_SALT,
                max_age=CONNECTION_MAX_AGE,
            )
        except signing.BadSignature:
            identity = None
        if identity != self.connection_identity(options, source_url):
            messages.error(
                self.request, _("Connect again before you migrate a conference.")
            )
            return self.render_to_response(self.get_context_data(form=form))
        try:
            conferences = fetch_conferences(**options)
            conference_form = FrabConferenceForm(
                self.request.POST, conferences=conferences
            )
            if conference_form.is_valid():
                conference_id = int(conference_form.cleaned_data["conference"])
                conference = fetch_conference(conference_id, **options)
                if self.request.POST.get("action") in ("select", "check_slug"):
                    initial = {
                        "conference": str(conference_id),
                        "connection": self.request.POST["connection"],
                    }
                    if self.request.POST.get("action") == "check_slug":
                        initial.update(
                            {
                                key: self.request.POST.get(key, "")
                                for key in (
                                    *missing_event_fields(conference),
                                    "import_tracks",
                                    "import_submission_types",
                                )
                            }
                        )
                    conference_form = FrabConferenceForm(
                        conferences=conferences, conference=conference, initial=initial
                    )
                else:
                    conference_form = FrabConferenceForm(
                        self.request.POST,
                        conferences=conferences,
                        conference=conference,
                    )
                    if conference_form.is_valid():
                        try:
                            migrate_conference(
                                conference,
                                organiser=self.request.organiser,
                                user=self.request.user,
                                source_url=source_url,
                                import_tracks=conference_form.cleaned_data.get(
                                    "import_tracks", True
                                ),
                                import_submission_types=conference_form.cleaned_data.get(
                                    "import_submission_types", True
                                ),
                                overrides={
                                    key: conference_form.cleaned_data[key]
                                    for key in conference_form.missing_fields
                                },
                            )
                        except ValidationError as exc:
                            errors = (
                                exc.message_dict
                                if hasattr(exc, "message_dict")
                                else {"": exc.messages}
                            )
                            if "slug" in errors and conference_form.check_slug():
                                errors["slug"] = [conference_form.slug_error]
                            for field, errors_for_field in errors.items():
                                for error in errors_for_field:
                                    if field in conference_form.fields:
                                        conference_form.add_error(field, error)
                                    else:
                                        conference_form.add_error(
                                            None,
                                            f"{field}: {error}" if field else error,
                                        )
                        except IntegrityError:
                            conference_form.add_error(
                                None,
                                conference_form.check_slug()
                                or _(
                                    "Could not create the event. Check whether its short form is already in use."
                                ),
                            )
                        else:
                            messages.success(
                                self.request, _("The event was created. It is public.")
                            )
                            return redirect(self.get_success_url())
        except FrabDatabaseError as exc:
            messages.error(self.request, str(exc))
            return self.render_to_response(self.get_context_data(form=form))
        if (
            conference_form
            and not conference_form.is_bound
            and conference_form.slug_error
        ):
            messages.error(self.request, conference_form.slug_error)
        return self.render_to_response(
            self.get_context_data(
                form=form, connected=True, conference_form=conference_form
            )
        )
