import pytest
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils.timezone import now
from eventyay.base.models import Event, Organizer, User
from eventyay.base.settings import GlobalSettingsObject
from unittest.mock import MagicMock

from eventyay_business.forms import CountryFeeSettingForm, GlobalFeeSettingsForm
from eventyay_business.models import (
    CountryFeeSetting,
    Subscription,
    Tier,
    TierEntitlement,
    TierVersion,
    UsageRecord,
)
from eventyay_business.services import get_event_country, resolve_fee_settings
from eventyay_business.signals import record_platform_fee_on_order_paid


@pytest.fixture
def admin_user(db):
    return User.objects.create_user(
        "admin@example.com", "dummy", is_staff=True, is_superuser=True
    )


@pytest.fixture
def regular_user(db):
    return User.objects.create_user(
        "user@example.com", "dummy", is_staff=False, is_superuser=False
    )


@pytest.fixture
def sample_organizer(db):
    return Organizer.objects.create(name="Sample Org", slug="sample-org")


@pytest.fixture
def organizer_with_tier(db, sample_organizer):
    tier = Tier.objects.create(slug="standard", name="Standard Tier")
    version = TierVersion.objects.create(tier=tier, version=1, published_at=now())
    TierEntitlement.objects.create(
        tier_version=version,
        capability="commerce.platform_fee_percent",
        value="3.00",
    )
    sub = Subscription.objects.get(organizer=sample_organizer)
    sub.tier_version = version
    sub.save()
    return sample_organizer


@pytest.mark.django_db
class TestCountryFeeSettingModel:
    def test_create_and_str(self):
        setting = CountryFeeSetting.objects.create(
            country="DE",
            currency="EUR",
            service_fee_percent=Decimal("2.00"),
            maximum_fee=Decimal("50.00"),
        )
        assert setting.country == "DE"
        assert setting.currency == "EUR"
        assert setting.service_fee_percent == Decimal("2.00")
        assert setting.maximum_fee == Decimal("50.00")
        assert "DE (EUR): 2.00% (max: 50.00)" in str(setting)

    def test_normalization_to_uppercase(self):
        setting = CountryFeeSetting.objects.create(
            country="us",
            currency="usd",
            service_fee_percent=Decimal("1.50"),
            maximum_fee=Decimal("25.00"),
        )
        assert setting.country == "US"
        assert setting.currency == "USD"

    def test_validation_invalid_percentage(self):
        with pytest.raises(ValidationError):
            CountryFeeSetting.objects.create(
                country="US",
                currency="USD",
                service_fee_percent=Decimal("105.00"),
                maximum_fee=Decimal("10.00"),
            )

        with pytest.raises(ValidationError):
            CountryFeeSetting.objects.create(
                country="US",
                currency="USD",
                service_fee_percent=Decimal("-1.00"),
                maximum_fee=Decimal("10.00"),
            )

    def test_validation_invalid_maximum_fee(self):
        with pytest.raises(ValidationError):
            CountryFeeSetting.objects.create(
                country="US",
                currency="USD",
                service_fee_percent=Decimal("2.50"),
                maximum_fee=Decimal("-5.00"),
            )

    def test_unique_together(self):
        CountryFeeSetting.objects.create(
            country="IN",
            currency="INR",
            service_fee_percent=Decimal("2.00"),
            maximum_fee=Decimal("100.00"),
        )
        with pytest.raises(Exception):
            CountryFeeSetting.objects.create(
                country="IN",
                currency="INR",
                service_fee_percent=Decimal("3.00"),
                maximum_fee=Decimal("200.00"),
            )


@pytest.mark.django_db
class TestFeeSettingsForms:
    def test_country_fee_setting_form_valid(self):
        form = CountryFeeSettingForm(
            data={
                "country": "SG",
                "currency": "SGD",
                "service_fee_percent": "2.50",
                "maximum_fee": "30.00",
            }
        )
        assert form.is_valid()
        instance = form.save()
        assert instance.country == "SG"
        assert instance.currency == "SGD"

    def test_country_fee_setting_form_invalid_currency(self):
        form = CountryFeeSettingForm(
            data={
                "country": "SG",
                "currency": "INVALID",
                "service_fee_percent": "2.50",
                "maximum_fee": "30.00",
            }
        )
        assert not form.is_valid()
        assert "currency" in form.errors

    def test_global_fee_settings_form_save(self):
        gs = GlobalSettingsObject()
        gs.settings.set("ticket_fee_percentage", "2.50")
        gs.settings.set("ticket_fee_maximum", "100.00")

        form = GlobalFeeSettingsForm(
            data={
                "ticket_fee_percentage": "3.50",
                "ticket_fee_maximum": "150.00",
            }
        )
        assert form.is_valid()
        form.save()

        assert gs.settings.get("ticket_fee_percentage") == "3.50"
        assert gs.settings.get("ticket_fee_maximum") == "150.00"


@pytest.mark.django_db
class TestFeeResolutionService:
    def test_country_override_takes_precedence(self, organizer_with_tier):
        CountryFeeSetting.objects.create(
            country="DE",
            currency="EUR",
            service_fee_percent=Decimal("1.25"),
            maximum_fee=Decimal("40.00"),
        )

        event = Event.objects.create(
            organizer=organizer_with_tier,
            name="DE Event",
            slug="de-event",
            currency="EUR",
            date_from=now(),
        )
        event.settings.set("invoice_address_from_country", "DE")

        sub = Subscription.objects.get(organizer=organizer_with_tier)
        pct, max_fee, is_override = resolve_fee_settings(
            event=event, tier_version=sub.tier_version
        )

        assert is_override is True
        assert pct == Decimal("1.25")
        assert max_fee == Decimal("40.00")

    def test_fallback_to_tier_when_no_country_override(self, organizer_with_tier):
        event = Event.objects.create(
            organizer=organizer_with_tier,
            name="Generic Event",
            slug="generic-event",
            currency="USD",
            date_from=now(),
        )
        event.settings.set("invoice_address_from_country", "US")

        gs = GlobalSettingsObject()
        gs.settings.set("ticket_fee_maximum", "80.00")

        sub = Subscription.objects.get(organizer=organizer_with_tier)
        pct, max_fee, is_override = resolve_fee_settings(
            event=event, tier_version=sub.tier_version
        )

        assert is_override is False
        assert pct == Decimal("3.00")
        assert max_fee == Decimal("80.00")

    def test_fallback_to_global_settings_without_tier(self, sample_organizer):
        event = Event.objects.create(
            organizer=sample_organizer,
            name="No Tier Event",
            slug="no-tier-event",
            currency="GBP",
            date_from=now(),
        )

        gs = GlobalSettingsObject()
        gs.settings.set("ticket_fee_percentage", "2.75")
        gs.settings.set("ticket_fee_maximum", "55.00")

        pct, max_fee, is_override = resolve_fee_settings(event=event, tier_version=None)

        assert is_override is False
        assert pct == Decimal("2.75")
        assert max_fee == Decimal("55.00")


@pytest.mark.django_db
class TestPlatformFeeWithCountryOverrides:
    def test_order_paid_applies_country_override_and_caps_fee(
        self, organizer_with_tier
    ):
        CountryFeeSetting.objects.create(
            country="FR",
            currency="EUR",
            service_fee_percent=Decimal("2.00"),
            maximum_fee=Decimal("15.00"),
        )

        event = Event.objects.create(
            organizer=organizer_with_tier,
            name="Paris Conference",
            slug="paris-conf",
            currency="EUR",
            date_from=now(),
        )
        event.settings.set("invoice_address_from_country", "FR")

        order = MagicMock()
        order.code = "FEE123"
        order.total = Decimal("2000.00")
        del order.invoice_address
        order.positions.all.return_value = [
            MagicMock(price=Decimal("2000.00"), tax_value=Decimal("0.00"))
        ]

        # 2% of 2000 = 40.00 EUR, but capped at 15.00 EUR
        record_platform_fee_on_order_paid(sender=event, order=order)

        record = UsageRecord.objects.get(source_id="FEE123")
        assert record.quantity == Decimal("15.00")
        assert record.metadata["maximum_fee"] == "15.00"
        assert record.metadata["is_fee_override"] == "True"

    def test_order_paid_without_cap_when_maximum_fee_zero(self, organizer_with_tier):
        CountryFeeSetting.objects.create(
            country="JP",
            currency="JPY",
            service_fee_percent=Decimal("3.00"),
            maximum_fee=Decimal("0.00"),  # Unlimited
        )

        event = Event.objects.create(
            organizer=organizer_with_tier,
            name="Tokyo Event",
            slug="tokyo-event",
            currency="JPY",
            date_from=now(),
        )
        event.settings.set("invoice_address_from_country", "JP")

        order = MagicMock()
        order.code = "JPY999"
        order.total = Decimal("100000.00")
        del order.invoice_address
        order.positions.all.return_value = [
            MagicMock(price=Decimal("100000.00"), tax_value=Decimal("0.00"))
        ]

        # 3% of 100,000 = 3000 JPY, not capped
        record_platform_fee_on_order_paid(sender=event, order=order)

        record = UsageRecord.objects.get(source_id="JPY999")
        assert record.quantity == Decimal("3000.00")


@pytest.fixture
def staff_client(client, admin_user):
    client.force_login(admin_user)
    admin_user.staffsession_set.create(
        date_start=now(), session_key=client.session.session_key
    )
    return client


@pytest.mark.django_db
class TestFeeSettingsViews:
    def test_fee_settings_list_view_admin(self, staff_client):
        CountryFeeSetting.objects.create(
            country="US",
            currency="USD",
            service_fee_percent=Decimal("2.00"),
            maximum_fee=Decimal("50.00"),
        )
        url = reverse("plugins:eventyay_business:fees.list")
        response = staff_client.get(url)
        assert response.status_code == 200
        assert "global_form" in response.context_data
        assert len(response.context_data["country_settings"]) == 1

    def test_fee_settings_create_view_admin(self, staff_client):
        data = {
            "country": "CA",
            "currency": "CAD",
            "service_fee_percent": "1.75",
            "maximum_fee": "45.00",
        }
        url = reverse("plugins:eventyay_business:fees.add")
        response = staff_client.post(url, data=data)
        assert response.status_code == 302
        assert CountryFeeSetting.objects.filter(country="CA", currency="CAD").exists()

    def test_fee_settings_update_view_admin(self, staff_client):
        setting = CountryFeeSetting.objects.create(
            country="ES",
            currency="EUR",
            service_fee_percent=Decimal("1.50"),
            maximum_fee=Decimal("20.00"),
        )
        data = {
            "country": "ES",
            "currency": "EUR",
            "service_fee_percent": "2.25",
            "maximum_fee": "35.00",
        }
        url = reverse("plugins:eventyay_business:fees.edit", kwargs={"pk": setting.pk})
        response = staff_client.post(url, data=data)
        assert response.status_code == 302
        setting.refresh_from_db()
        assert setting.service_fee_percent == Decimal("2.25")
        assert setting.maximum_fee == Decimal("35.00")

    def test_fee_settings_delete_view_admin(self, staff_client):
        setting = CountryFeeSetting.objects.create(
            country="IT",
            currency="EUR",
            service_fee_percent=Decimal("2.10"),
            maximum_fee=Decimal("30.00"),
        )
        url = reverse(
            "plugins:eventyay_business:fees.delete", kwargs={"pk": setting.pk}
        )
        response = staff_client.post(url)
        assert response.status_code == 302
        assert not CountryFeeSetting.objects.filter(pk=setting.pk).exists()

    def test_get_event_country(self, sample_organizer):
        event = Event.objects.create(
            organizer=sample_organizer,
            name="Country Test Event",
            slug="country-test-event",
            currency="EUR",
            date_from=now(),
        )
        event.settings.set("invoice_address_from_country", "FR")
        assert get_event_country(event=event) == "FR"
