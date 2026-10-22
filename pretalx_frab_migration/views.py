import json

from django.contrib import messages
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.shortcuts import redirect
from django.utils.crypto import salted_hmac
from django.utils.translation import gettext_lazy as _
from django.views.generic import FormView
from django_scopes import scopes_disabled

from pretalx.common.models.settings import GlobalSettings
from pretalx.common.text.phrases import phrases
from pretalx.common.ui import Button
from pretalx.common.views.mixins import PermissionRequired
from pretalx.event.models import Event
from pretalx.person.models import SpeakerProfile, User

from .database import (
    FrabDatabaseError,
    fetch_conference,
    fetch_conferences,
    fetch_speakers,
)
from .forms import FrabConferenceForm, FrabMigrationSettingsForm
from .import_state import SOURCE_KEY, find_import, load_import, lock_source, save_import
from .migration import migrate_conference, missing_event_fields
from .speaker_forms import SpeakerImportForm
from .speakers import apply_speaker_import

CONNECTION_SALT = "pretalx_frab_migration.connection"
CONNECTION_MAX_AGE = 3600
PREVIEW_SALT = "pretalx_frab_migration.speakers"
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
        if request.POST.get("action") in ("select", "check_slug", "review", "migrate"):
            return self.migrate()
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        if self.request.POST.get("action") == "connect":
            return self.connect(form)
        form.save()
        messages.success(self.request, phrases.base.saved)
        return super().form_valid(form)

    def connection_identity(self, options, source_url):
        fingerprint = salted_hmac(
            CONNECTION_SALT,
            json.dumps(
                {
                    **options,
                    "url": source_url,
                    "source": GlobalSettings().settings.get(
                        "frab_migration_source_id", default=""
                    ),
                },
                sort_keys=True,
            ),
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

    def selected_mapping(self, source_id, conference_id):
        mapping = find_import(source_id, conference_id, self.request.organiser)
        events = []
        if not mapping:
            events = list(
                Event.objects.filter(organiser=self.request.organiser)
                .exclude(_settings_objects__key=SOURCE_KEY)
                .order_by("slug")
            )
            selected = self.request.POST.get("existing_event")
            if selected:
                event = next((e for e in events if str(e.pk) == selected), None)
                if not event:
                    raise ValidationError(
                        _("Select an unlinked event from this organiser.")
                    )
                mapping = load_import(
                    event, source_id=source_id, conference_id=conference_id
                )
        return mapping, events

    def preview_token(self, identity, conference_id, preview):
        return signing.dumps(
            [identity, conference_id, preview.fingerprint], salt=PREVIEW_SALT
        )

    def check_preview(self, identity, conference_id, preview):
        if not preview.people and not preview.mapping:
            return
        try:
            value = signing.loads(
                self.request.POST.get("speakers_preview", ""),
                salt=PREVIEW_SALT,
                max_age=CONNECTION_MAX_AGE,
            )
        except signing.BadSignature:
            value = None
        if value != [identity, conference_id, preview.fingerprint]:
            raise ValidationError(
                _(
                    "The speaker preview is new or has changed. Review it before you migrate."
                )
            )

    def commit_import(
        self,
        *,
        source_id,
        conference_id,
        conference_form,
        preview,
        options,
        source_url,
        identity,
    ):
        with transaction.atomic(), scopes_disabled():
            lock_source(source_id)
            mapping, _events = self.selected_mapping(source_id, conference_id)
            if mapping and mapping.event_id:
                Event.objects.select_for_update().get(pk=mapping.event_id)
                list(
                    SpeakerProfile.objects.filter(
                        event=mapping.event
                    ).select_for_update()
                )
            list(User.objects.filter(pk__in=preview.accounts).select_for_update())
            conference = fetch_conference(conference_id, **options)
            people = fetch_speakers(conference_id, **options)
            current = SpeakerImportForm(
                self.request.POST,
                conference=conference,
                people=people,
                mapping=mapping,
                source_url=source_url,
            )
            if current.fingerprint != preview.fingerprint:
                raise ValidationError(
                    _(
                        "The source or speaker data changed. Update the preview and try again."
                    )
                )
            self.check_preview(identity, conference_id, current)
            if not current.is_valid():
                raise ValidationError(
                    [
                        str(error)
                        for errors in current.errors.values()
                        for error in errors
                    ]
                )
            created = not mapping or not mapping.event_id
            if created:
                try:
                    event = migrate_conference(
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
                except (ValidationError, IntegrityError):
                    if conference_form.check_slug():
                        raise ValidationError(
                            {"slug": conference_form.slug_error}
                        ) from None
                    raise
                mapping = load_import(
                    event, source_id=source_id, conference_id=conference_id
                )
            save_import(mapping)
            counts = apply_speaker_import(current, mapping, self.request.user)
            return created, counts

    def migrate(self):
        form = self.form_class()
        options = {
            key: form.obj.settings.get(f"frab_migration_db_{key}", default="") or ""
            for key in DATABASE_KEYS
        }
        options["port"] = options["port"] or None
        source_url = form.obj.settings.get("frab_migration_url", default="") or ""
        source_id = form.obj.settings.get("frab_migration_source_id", default="")
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
        conference_form = None
        preview = None
        preview_token = ""
        try:
            with scopes_disabled():
                conferences = fetch_conferences(**options)
                conference_form = FrabConferenceForm(
                    self.request.POST, conferences=conferences
                )
                if conference_form.is_valid():
                    conference_id = int(conference_form.cleaned_data["conference"])
                    conference = fetch_conference(conference_id, **options)
                    people = fetch_speakers(conference_id, **options)
                    mapping, existing_events = self.selected_mapping(
                        source_id, conference_id
                    )
                    action = self.request.POST.get("action")
                    initial = {
                        "conference": str(conference_id),
                        "connection": self.request.POST["connection"],
                        "existing_event": self.request.POST.get("existing_event", ""),
                    }
                    if action != "select":
                        initial.update(
                            {
                                key: self.request.POST.get(key, "")
                                for key in (
                                    *missing_event_fields(conference),
                                    "import_tracks",
                                    "import_submission_types",
                                    "existing_event",
                                )
                            }
                        )
                    conference_form = FrabConferenceForm(
                        self.request.POST if action in ("migrate", "review") else None,
                        conferences=conferences,
                        conference=conference,
                        initial=initial,
                        event=mapping.event if mapping and mapping.event_id else None,
                        existing_events=existing_events,
                    )
                    preview = SpeakerImportForm(
                        self.request.POST
                        if action in ("migrate", "review", "check_slug")
                        else None,
                        conference=conference,
                        people=people,
                        mapping=mapping,
                        source_url=source_url,
                    )
                    preview_token = self.preview_token(identity, conference_id, preview)
                    if (
                        action == "migrate"
                        and conference_form.is_valid()
                        and preview.is_valid()
                    ):
                        try:
                            self.check_preview(identity, conference_id, preview)
                            created, counts = self.commit_import(
                                source_id=source_id,
                                conference_id=conference_id,
                                conference_form=conference_form,
                                preview=preview,
                                options=options,
                                source_url=source_url,
                                identity=identity,
                            )
                        except ValidationError as exc:
                            errors = (
                                exc.message_dict
                                if hasattr(exc, "message_dict")
                                else {"": exc.messages}
                            )
                            if "slug" in errors and (
                                conference_form.slug_error
                                or conference_form.check_slug()
                            ):
                                errors["slug"] = [conference_form.slug_error]
                            for field, values in errors.items():
                                for error in values:
                                    conference_form.add_error(
                                        field
                                        if field in conference_form.fields
                                        else None,
                                        f"{field}: {error}"
                                        if field and field not in conference_form.fields
                                        else error,
                                    )
                        except (IntegrityError, OperationalError):
                            conference_form.add_error(
                                None,
                                conference_form.check_slug()
                                or _(
                                    "Could not create the event or update its speakers. Update the preview and try again."
                                ),
                            )
                        else:
                            if created:
                                messages.success(
                                    self.request,
                                    _("The event was created. It is public."),
                                )
                            messages.success(
                                self.request,
                                _(
                                    "Speakers: %(managed)s managed, %(linked)s linked, %(reused)s reused, %(merged)s merged, %(skipped)s unchanged."
                                )
                                % {
                                    key: counts[key]
                                    for key in (
                                        "managed",
                                        "linked",
                                        "reused",
                                        "merged",
                                        "skipped",
                                    )
                                },
                            )
                            if people and not source_url:
                                messages.info(
                                    self.request,
                                    _(
                                        "Source links were omitted because the frab base URL is empty."
                                    ),
                                )
                            return redirect(self.get_success_url())
        except FrabDatabaseError as exc:
            messages.error(self.request, str(exc))
            return self.render_to_response(self.get_context_data(form=form))
        except OperationalError:
            messages.error(
                self.request,
                _("The database is busy. Update the preview and try again."),
            )
        except ValidationError as exc:
            messages.error(self.request, " ".join(exc.messages))
        if (
            conference_form
            and not conference_form.is_bound
            and conference_form.slug_error
        ):
            messages.error(self.request, conference_form.slug_error)
        return self.render_to_response(
            self.get_context_data(
                form=form,
                connected=True,
                conference_form=conference_form,
                speaker_form=preview,
                speakers_preview=preview_token,
            )
        )
