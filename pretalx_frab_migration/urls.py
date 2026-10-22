from django.urls import path

from .views import FrabMigrationView

urlpatterns = [
    path(
        "orga/organiser/<slug:organiser>/p/pretalx_frab_migration/",
        FrabMigrationView.as_view(),
        name="settings",
    )
]
