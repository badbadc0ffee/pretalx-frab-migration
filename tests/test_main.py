import pytest
from django.urls import reverse

from pretalx.common.models.settings import GlobalSettings
from pretalx.event.models import Organiser

SETTINGS_URL_NAME = "plugins:pretalx_frab_migration:settings"
FRAB_URL_SETTING = "frab_migration_url"

pytestmark = pytest.mark.django_db


def test_admin_can_access_settings_without_an_event(admin_client, settings_url):
    response = admin_client.get(settings_url)

    assert response.status_code == 200
    assert list(response.context["form"].fields) == [
        FRAB_URL_SETTING,
        "frab_migration_db_backend",
        "frab_migration_db_name",
        "frab_migration_db_host",
        "frab_migration_db_port",
        "frab_migration_db_user",
        "frab_migration_db_password",
    ]
    assert response.context["form"][FRAB_URL_SETTING].value() in (None, "")
    assert GlobalSettings().settings.get(FRAB_URL_SETTING) is None
    assert response.wsgi_request.organiser is not None
    assert response.wsgi_request.event is None


@pytest.mark.parametrize(
    "frab_url", ("https://frab.example.org/", "http://frab.example.org/frab/")
)
def test_admin_can_save_global_settings(admin_client, frab_url, settings_url):
    url = settings_url
    response = admin_client.post(url, {FRAB_URL_SETTING: frab_url})

    assert response.status_code == 302
    assert response.url == url
    assert GlobalSettings().settings.get(FRAB_URL_SETTING) == frab_url

    response = admin_client.get(url)
    assert response.context["form"][FRAB_URL_SETTING].value() == frab_url


def test_admin_can_change_and_clear_global_settings(admin_client, settings_url):
    url = settings_url
    GlobalSettings().settings.set(FRAB_URL_SETTING, "https://old.example.org/")

    response = admin_client.post(url, {FRAB_URL_SETTING: "https://new.example.org/"})
    assert response.status_code == 302
    assert GlobalSettings().settings.get(FRAB_URL_SETTING) == "https://new.example.org/"

    response = admin_client.post(url, {FRAB_URL_SETTING: ""})
    assert response.status_code == 302
    assert GlobalSettings().settings.get(FRAB_URL_SETTING) == ""


@pytest.mark.parametrize(
    "frab_url", ("not a URL", "ftp://frab.example.org/", "javascript:alert(1)")
)
def test_invalid_url_does_not_change_settings(admin_client, frab_url, settings_url):
    saved_url = "https://frab.example.org/"
    GlobalSettings().settings.set(FRAB_URL_SETTING, saved_url)

    response = admin_client.post(settings_url, {FRAB_URL_SETTING: frab_url})

    assert response.status_code == 200
    assert FRAB_URL_SETTING in response.context["form"].errors
    assert GlobalSettings().settings.get(FRAB_URL_SETTING) == saved_url


@pytest.mark.parametrize("client_fixture", ("orga_client", "review_client"))
@pytest.mark.parametrize("method", ("get", "post"))
def test_event_users_cannot_access_global_settings(
    request, client_fixture, method, settings_url
):
    client = request.getfixturevalue(client_fixture)
    saved_url = "https://frab.example.org/"
    GlobalSettings().settings.set(FRAB_URL_SETTING, saved_url)
    password_key = "frab_migration_db_password"
    GlobalSettings().settings.set(password_key, "saved-database-secret")

    response = getattr(client, method)(
        settings_url,
        {
            FRAB_URL_SETTING: "https://other.example.org/",
            "frab_migration_db_backend": "mysql",
            "frab_migration_db_name": "other_db",
            password_key: "other-database-secret",
        },
    )

    assert response.status_code == 404
    assert GlobalSettings().settings.get(FRAB_URL_SETTING) == saved_url
    assert GlobalSettings().settings.get(password_key) == "saved-database-secret"
    assert b"saved-database-secret" not in response.content


@pytest.mark.parametrize("method", ("get", "post"))
def test_anonymous_user_must_sign_in(client, method, settings_url):
    response = getattr(client, method)(
        settings_url, {FRAB_URL_SETTING: "https://frab.example.org/"}
    )

    assert response.status_code == 302
    assert response.url.startswith(reverse("orga:login"))
    assert GlobalSettings().settings.get(FRAB_URL_SETTING) is None


@pytest.mark.parametrize("page", ("settings", "dashboard"))
def test_organiser_navigation_links_to_settings(
    admin_client, organiser, page, settings_url
):
    url = settings_url
    page_url = (
        url
        if page == "settings"
        else reverse("orga:organiser.dashboard", kwargs={"organiser": organiser.slug})
    )
    response = admin_client.get(page_url)
    entries = [entry for entry in response.context["nav_global"] if entry["url"] == url]

    assert len(entries) == 1
    assert entries[0]["active"] is (page == "settings")
    assert response.wsgi_request.organiser == organiser
    assert response.wsgi_request.event is None


@pytest.mark.parametrize(
    "client_fixture", ("admin_client", "orga_client", "review_client")
)
def test_global_navigation_hides_settings(request, client_fixture, settings_url):
    client = request.getfixturevalue(client_fixture)
    response = client.get(reverse("orga:event.list"))

    assert response.status_code == 200
    assert all(entry["url"] != settings_url for entry in response.context["nav_global"])


def test_event_settings_link_is_removed(admin_client, event):
    response = admin_client.get(
        f"/orga/event/{event.slug}/settings/p/pretalx_frab_migration/"
    )

    assert response.status_code == 404


@pytest.mark.parametrize("client_fixture", ("orga_client", "review_client"))
def test_organiser_navigation_hides_settings_from_non_admins(
    request, client_fixture, organiser, settings_url
):
    client = request.getfixturevalue(client_fixture)
    response = client.get(
        reverse("orga:organiser.dashboard", kwargs={"organiser": organiser.slug})
    )

    assert response.status_code == 200
    assert all(entry["url"] != settings_url for entry in response.context["nav_global"])


def test_event_navigation_hides_settings(admin_client, event, settings_url):
    response = admin_client.get(event.orga_urls.base)

    assert response.status_code == 200
    assert settings_url not in response.content.decode()


@pytest.mark.parametrize("method", ("get", "post"))
@pytest.mark.parametrize("organiser_slug", (None, "missing-organiser"))
def test_settings_require_an_existing_organiser(admin_client, method, organiser_slug):
    url = (
        reverse(SETTINGS_URL_NAME, kwargs={"organiser": organiser_slug})
        if organiser_slug
        else "/orga/p/pretalx_frab_migration/settings/"
    )
    response = getattr(admin_client, method)(
        url, {FRAB_URL_SETTING: "https://frab.example.org/"}
    )

    assert response.status_code == 404
    assert GlobalSettings().settings.get(FRAB_URL_SETTING) is None


def test_settings_stay_global_across_organisers(admin_client, organiser, settings_url):
    other = Organiser.objects.create(name="Other organiser", slug="other-organiser")
    other_url = reverse(SETTINGS_URL_NAME, kwargs={"organiser": other.slug})
    response = admin_client.post(
        settings_url, {FRAB_URL_SETTING: "https://frab.example.org/"}
    )
    assert response.status_code == 302
    assert response.url == settings_url

    response = admin_client.get(other_url)
    assert response.status_code == 200
    assert response.wsgi_request.organiser == other
    assert (
        response.context["form"][FRAB_URL_SETTING].value()
        == "https://frab.example.org/"
    )
    links = [entry["url"] for entry in response.context["nav_global"]]
    assert other_url in links
    assert settings_url not in links
