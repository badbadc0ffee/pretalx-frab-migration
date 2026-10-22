from i18nfield.forms import I18nModelForm

from .models import FrabMigrationSettings


class FrabMigrationSettingsForm(I18nModelForm):
    def __init__(self, *args, event=None, **kwargs):
        self.instance, _ = FrabMigrationSettings.objects.get_or_create(event=event)
        super().__init__(*args, **kwargs, instance=self.instance, locales=event.locales)

    class Meta:
        model = FrabMigrationSettings
        fields = ("some_setting",)
        widgets = {}
