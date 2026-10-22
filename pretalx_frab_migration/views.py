from django.contrib import messages
from django.views.generic import FormView

from pretalx.common.text.phrases import phrases
from pretalx.common.views.mixins import PermissionRequired

from .forms import FrabMigrationSettingsForm


class FrabMigrationSettingsView(PermissionRequired, FormView):
    permission_required = "person.administrator_user"
    template_name = "pretalx_frab_migration/settings.html"
    form_class = FrabMigrationSettingsForm

    def get_success_url(self):
        return self.request.path

    def form_valid(self, form):
        form.save()
        messages.success(self.request, phrases.base.saved)
        return super().form_valid(form)
