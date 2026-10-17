import pytest
from django.urls import resolve, reverse
from django.utils.timezone import now
from eventyay.base.models import User

from eventyay_business.forms import GlobalBusinessSettingsForm


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "valid_key", "invalid_key"),
    [
        ("payment_stripe_publishable_key", "pk_live_valid", "pk_test_wrong"),
        ("payment_stripe_secret_key", "sk_live_valid", "sk_test_wrong"),
        ("payment_stripe_test_publishable_key", "pk_test_valid", "pk_live_wrong"),
        ("payment_stripe_test_secret_key", "rk_test_valid", "rk_live_wrong"),
    ],
)
def test_stripe_key_prefixes(field, valid_key, invalid_key):
    valid_form = GlobalBusinessSettingsForm(data={field: valid_key})
    assert valid_form.is_valid(), valid_form.errors

    invalid_form = GlobalBusinessSettingsForm(data={field: invalid_key})
    assert not invalid_form.is_valid()
    assert field in invalid_form.errors


@pytest.mark.django_db
def test_business_settings_sidebar_target_opens_plugin_page(client):
    url = reverse("plugins:eventyay_business:settings")
    if reverse("eventyay_admin:admin.global.business") == url:
        pytest.skip("Core still owns the old business settings URL")

    user = User.objects.create_user(
        "business-admin@example.com", "password", is_staff=True
    )
    client.force_login(user)
    user.staffsession_set.create(
        date_start=now(), session_key=client.session.session_key
    )

    assert resolve(url).namespace == "plugins:eventyay_business"
    response = client.get(url)
    assert response.status_code == 200
    assert b"Business Settings" in response.content


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["", "1, 3, 5", "2, 7"])
def test_collection_retry_schedule_accepts_bounded_ascending_days(value):
    form = GlobalBusinessSettingsForm(data={"business_collection_retry_days": value})

    assert form.is_valid(), form.errors
    assert form.cleaned_data["business_collection_retry_days"] == (value or "1, 3, 5")


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["0, 3", "3, 1", "1, 1", "one, two"])
def test_collection_retry_schedule_rejects_invalid_values(value):
    form = GlobalBusinessSettingsForm(data={"business_collection_retry_days": value})

    assert not form.is_valid()
    assert "business_collection_retry_days" in form.errors
