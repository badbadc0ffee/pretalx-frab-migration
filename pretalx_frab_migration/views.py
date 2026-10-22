from django.contrib import messages
from django.utils.translation import gettext_lazy as _
from django.views.generic import FormView

from pretalx.common.views.mixins import PermissionRequired

from .forms import FrabMigrationSettingsForm


class FrabMigrationSettingsView(PermissionRequired, FormView):
    permission_required = "event.update_event"
    template_name = "pretalx_frab_migration/settings.html"
    form_class = FrabMigrationSettingsForm

    def get_success_url(self):
        return self.request.path

    def get_object(self):
        return self.request.event

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["event"] = self.request.event
        return kwargs

    def form_valid(self, form):
        form.save()
        messages.success(self.request, _("The frab-migration settings were updated."))
        return super().form_valid(form)
