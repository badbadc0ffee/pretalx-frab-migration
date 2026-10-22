from django.contrib import messages
from django.views.generic import FormView

from pretalx.common.text.phrases import phrases
from pretalx.common.views.mixins import PermissionRequired

from .database import FrabDatabaseError, fetch_conferences
from .forms import FrabConferenceForm, FrabMigrationSettingsForm


class FrabMigrationView(PermissionRequired, FormView):
    permission_required = "person.administrator_user"
    template_name = "pretalx_frab_migration/settings.html"
    form_class = FrabMigrationSettingsForm

    def get_success_url(self):
        return self.request.path

    def form_valid(self, form):
        if self.request.POST.get("action") == "connect":
            return self.connect(form)
        form.save()
        messages.success(self.request, phrases.base.saved)
        return super().form_valid(form)

    def connect(self, form):
        options = {
            key: form.cleaned_data[f"frab_migration_db_{key}"]
            for key in ("backend", "name", "host", "port", "user", "password")
        }
        try:
            conferences = fetch_conferences(**options)
        except FrabDatabaseError as exc:
            return self.render_to_response(
                self.get_context_data(form=form, connection_error=str(exc))
            )
        form.save()
        conference_form = (
            FrabConferenceForm(conferences=conferences) if conferences else None
        )
        return self.render_to_response(
            self.get_context_data(
                form=form, connected=True, conference_form=conference_form
            )
        )
