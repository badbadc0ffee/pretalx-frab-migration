from django.urls import path

from .views import FrabMigrationSettingsView

urlpatterns = [
    path(
        "orga/organiser/<slug:organiser>/p/pretalx_frab_migration/",
        FrabMigrationSettingsView.as_view(),
        name="settings",
    )
]
