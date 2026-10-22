from django.dispatch import receiver
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from pretalx.orga.signals import nav_global


@receiver(nav_global)
def frab_migration_settings(sender, request, **kwargs):
    if (
        not getattr(request, "organiser", None)
        or getattr(request, "event", None)
        or not request.user.has_perm("person.administrator_user", None)
    ):
        return []

    return [
        {
            "label": _("frab Migration"),
            "icon": "database",
            "url": reverse(
                "plugins:pretalx_frab_migration:settings",
                kwargs={"organiser": request.organiser.slug},
            ),
            "active": request.resolver_match.view_name
            == "plugins:pretalx_frab_migration:settings",
        }
    ]
