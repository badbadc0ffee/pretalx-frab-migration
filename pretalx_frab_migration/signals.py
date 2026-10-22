from django.dispatch import receiver
from django.urls import reverse

from pretalx.orga.signals import nav_event_settings


@receiver(nav_event_settings)
def pretalx_frab_migration_settings(sender, request, **kwargs):
    return [
        {
            "label": "frab-migration",
            "url": reverse(
                "plugins:pretalx_frab_migration:settings",
                kwargs={"event": request.event.slug},
            ),
            "active": request.resolver_match.url_name
            == "plugins:pretalx_frab_migration:settings",
        }
    ]
