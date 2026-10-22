from django.apps import AppConfig
from django.utils.translation import gettext_lazy

from . import __version__


class PluginApp(AppConfig):
    name = "pretalx_frab_migration"
    verbose_name = "frab-migration"

    class PretalxPluginMeta:
        name = gettext_lazy("frab-migration")
        author = "Florian Mösch"
        description = gettext_lazy("Import conferences from frab into pretalx.")
        visible = False
        version = __version__
        category = "INTEGRATION"

    def ready(self):
        from . import signals  # noqa: PLC0415, F401
