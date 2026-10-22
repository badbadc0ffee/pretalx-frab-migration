import pytest
from django.urls import reverse

from pretalx_frab_migration.models import FrabMigrationSettings

SETTINGS_URL_NAME = "plugins:pretalx_frab_migration:settings"


@pytest.mark.django_db
def test_orga_can_access_settings(orga_client, event):
    response = orga_client.get(
        reverse(SETTINGS_URL_NAME, kwargs={"event": event.slug}), follow=True
    )
    assert response.status_code == 200


@pytest.mark.django_db
def test_orga_can_save_settings(orga_client, event):
    url = reverse(SETTINGS_URL_NAME, kwargs={"event": event.slug})
    response = orga_client.post(url, {"some_setting": "B"}, follow=True)
    assert response.status_code == 200
    assert FrabMigrationSettings.objects.get(event=event).some_setting == "B"


@pytest.mark.django_db
def test_reviewer_cannot_access_settings(review_client, event):
    response = review_client.get(
        reverse(SETTINGS_URL_NAME, kwargs={"event": event.slug})
    )
    assert response.status_code == 404
