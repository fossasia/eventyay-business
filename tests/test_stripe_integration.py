import pytest
from datetime import timedelta
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.test import RequestFactory, override_settings
from django.urls import reverse
from django.utils.timezone import now
from eventyay.base.models import Event, Organizer, User
from eventyay.base.models.auth import StaffSession
from eventyay.base.models.organizer import OrganizerBillingModel
from unittest.mock import MagicMock, call, patch

from eventyay_business.models import (
    AddonAssignmentScope,
    AddonDefinition,
    AddonPricingMode,
    AddonStatus,
    BillingCollectionAuthorization,
    BillingInterval,
    BusinessInvoice,
    BusinessInvoiceStatus,
    EventAddon,
    InvoiceLineType,
    OrganizerAddon,
    Subscription,
    SubscriptionStatus,
    Tier,
    TierPrice,
    TierStatus,
    TierVersion,
)
from eventyay_business.platform_fee_ledger import BILLING_CUTOVER_SETTING
from eventyay_business.signals import addon_purchased, subscription_purchased
from eventyay_business.stripe_service import (
    create_addon_checkout_session,
    create_subscription_checkout_session,
    get_or_create_stripe_customer,
    is_stripe_configured,
    process_invoice_paid,
    process_invoice_payment_failed,
    process_subscription_change,
    process_webhook_event,
    sync_addon_to_stripe,
    sync_organizer_from_stripe,
    sync_tier_price_to_stripe,
)
from eventyay_business.views_stripe import (
    StripeCheckoutCancelView,
    StripeCheckoutSuccessView,
    stripe_business_webhook_view,
)


@pytest.fixture
def setup_data(db):
    organizer = Organizer.objects.create(name="Stripe Org", slug="stripe-org")
    event = Event.objects.create(
        organizer=organizer,
        name="Stripe Event",
        slug="stripe-event",
        date_from=now(),
        live=True,
    )
    user = User.objects.create_user(
        email="organizer@stripe-org.com", password="secretpassword"
    )

    tier = Tier.objects.create(
        name="Pro Tier", slug="pro-tier", status=TierStatus.PUBLISHED
    )
    tier_version = TierVersion.objects.create(tier=tier, version=1)
    tier_price = TierPrice.objects.create(
        tier_version=tier_version,
        billing_interval=BillingInterval.MONTHLY,
        amount=Decimal("99.00"),
        currency="USD",
    )

    recurring_addon = AddonDefinition.objects.create(
        name="Extra Admin Seats",
        slug="extra-admin-seats",
        capability="teams.members.max",
        entitlement_value="5",
        price=Decimal("25.00"),
        currency="USD",
        active=True,
        public=True,
        pricing_mode=AddonPricingMode.RECURRING,
        assignment_scope=AddonAssignmentScope.ORGANIZER,
    )

    one_time_event_addon = AddonDefinition.objects.create(
        name="Event Booth Module",
        slug="event-booth-module",
        capability="exhibition.booths",
        entitlement_value="true",
        price=Decimal("15.00"),
        currency="USD",
        active=True,
        public=True,
        pricing_mode=AddonPricingMode.ONE_TIME,
        assignment_scope=AddonAssignmentScope.EVENT,
    )

    free_addon = AddonDefinition.objects.create(
        name="Free Analytics",
        slug="free-analytics",
        capability="analytics.basic",
        entitlement_value="true",
        price=Decimal("0.00"),
        currency="USD",
        active=True,
        public=True,
        pricing_mode=AddonPricingMode.ONE_TIME,
        assignment_scope=AddonAssignmentScope.ORGANIZER,
    )

    return (
        organizer,
        event,
        user,
        tier,
        tier_version,
        tier_price,
        recurring_addon,
        one_time_event_addon,
        free_addon,
    )


@pytest.mark.django_db
def test_is_stripe_configured_logic():
    with patch(
        "eventyay_business.stripe_service.get_stripe_secret_key_safe",
        return_value=None,
    ):
        assert is_stripe_configured() is False

    with patch(
        "eventyay.helpers.stripe_utils.get_stripe_secret_key",
        return_value="sk_test_12345",
    ):
        assert is_stripe_configured() is True


@pytest.mark.django_db
def test_get_or_create_stripe_customer(setup_data):
    organizer, _, user, _, _, _, _, _, _ = setup_data

    # When stripe is not configured
    with patch(
        "eventyay_business.stripe_service.get_stripe_secret_key_safe", return_value=None
    ):
        assert get_or_create_stripe_customer(organizer, user=user) is None

    # A legacy subscription reference is not canonical on its own.
    sub = organizer.subscriptions.filter(status=SubscriptionStatus.ACTIVE).first()
    if not sub:
        sub = Subscription.objects.create(
            organizer=organizer,
            tier_version=setup_data[4],
            status=SubscriptionStatus.ACTIVE,
            starts_at=now(),
        )
    sub.stripe_customer_id = "cus_existing_sub_123"
    sub.save()
    with patch(
        "eventyay_business.stripe_service.get_stripe_secret_key_safe",
        return_value="sk_test_123",
    ):
        with pytest.raises(
            ValidationError,
            match="Complete the organizer billing settings in Core before checkout",
        ):
            get_or_create_stripe_customer(organizer, user=user)

    OrganizerBillingModel.objects.create(
        organizer=organizer,
        primary_contact_name="Stripe Organizer",
        primary_contact_email="billing@example.com",
        company_or_organization_name="Stripe Org",
        address_line_1="Example Street 1",
        address_line_2="",
        city="Berlin",
        zip_code="10115",
        country="DE",
        preferred_language="en",
        tax_id="",
        stripe_customer_id="cus_canonical_456",
    )
    with patch(
        "eventyay_business.stripe_service.get_stripe_secret_key_safe",
        return_value="sk_test_123",
    ):
        assert (
            get_or_create_stripe_customer(organizer, user=user) == "cus_canonical_456"
        )

    sub.refresh_from_db()
    assert sub.stripe_customer_id == "cus_canonical_456"


@pytest.mark.django_db
def test_create_addon_checkout_session(setup_data):
    organizer, event, user, _, _, _, recurring_addon, one_time_event_addon, _ = (
        setup_data
    )

    with patch(
        "eventyay_business.stripe_service.get_stripe_secret_key_safe",
        return_value="sk_test_123",
    ):
        with patch(
            "eventyay_business.stripe_service.get_or_create_stripe_customer",
            return_value="cus_123",
        ):
            with patch("stripe.checkout.Session.create") as mock_session:
                mock_session.return_value = MagicMock(
                    url="https://checkout.stripe.com/c/pay/cs_test_123"
                )

                # Recurring organizer addon
                url = create_addon_checkout_session(
                    organizer=organizer,
                    addon=recurring_addon,
                    user=user,
                    quantity=2,
                    success_url="https://example.com/success",
                    cancel_url="https://example.com/cancel",
                )
                assert url == "https://checkout.stripe.com/c/pay/cs_test_123"
                args, kwargs = mock_session.call_args
                assert kwargs["mode"] == "subscription"
                assert kwargs["customer"] == "cus_123"
                assert kwargs["line_items"][0]["quantity"] == 2
                assert kwargs["line_items"][0]["price_data"]["unit_amount"] == 2500
                assert kwargs["metadata"]["type"] == "addon_purchase"
                assert kwargs["metadata"]["scope"] == "organizer"
                assert kwargs["metadata"]["organizer_slug"] == organizer.slug
                assert kwargs["subscription_data"]["payment_settings"] == {
                    "save_default_payment_method": "on_subscription"
                }
                assert "payment_method_types" not in kwargs

                # One-time event addon
                url_event = create_addon_checkout_session(
                    organizer=organizer,
                    addon=one_time_event_addon,
                    user=user,
                    quantity=1,
                    event=event,
                    success_url="https://example.com/success",
                    cancel_url="https://example.com/cancel",
                )
                assert url_event == "https://checkout.stripe.com/c/pay/cs_test_123"
                args, kwargs = mock_session.call_args
                assert kwargs["mode"] == "payment"
                assert kwargs["line_items"][0]["price_data"]["unit_amount"] == 1500
                assert kwargs["metadata"]["scope"] == "event"
                assert kwargs["metadata"]["event_slug"] == event.slug
                assert kwargs["payment_intent_data"]["setup_future_usage"] == (
                    "off_session"
                )
                assert "payment_method_types" not in kwargs


@pytest.mark.django_db
def test_create_subscription_checkout_session(setup_data):
    organizer, _, user, _, _, tier_price, _, _, _ = setup_data

    with patch(
        "eventyay_business.stripe_service.get_stripe_secret_key_safe",
        return_value="sk_test_123",
    ):
        with patch(
            "eventyay_business.stripe_service.get_or_create_stripe_customer",
            return_value="cus_123",
        ):
            with patch("stripe.checkout.Session.create") as mock_session:
                mock_session.return_value = MagicMock(
                    url="https://checkout.stripe.com/c/pay/cs_sub_test_123"
                )

                url = create_subscription_checkout_session(
                    organizer=organizer,
                    tier_price=tier_price,
                    user=user,
                    success_url="https://example.com/success",
                    cancel_url="https://example.com/cancel",
                )
                assert url == "https://checkout.stripe.com/c/pay/cs_sub_test_123"
                _, kwargs = mock_session.call_args
                assert kwargs["mode"] == "subscription"
                assert kwargs["customer"] == "cus_123"
                assert kwargs["line_items"][0]["price_data"]["unit_amount"] == 9900
                assert kwargs["metadata"]["type"] == "subscription"
                assert kwargs["metadata"]["scope"] == "organizer"
                assert kwargs["metadata"]["tier_price_id"] == str(tier_price.pk)
                assert kwargs["subscription_data"]["payment_settings"] == {
                    "save_default_payment_method": "on_subscription"
                }
                assert "payment_method_types" not in kwargs


@pytest.mark.django_db
def test_legacy_checkout_sessions_are_blocked_after_cutover(setup_data):
    organizer, _, user, _, _, tier_price, recurring_addon, _, _ = setup_data
    organizer.settings.set(BILLING_CUTOVER_SETTING, now().isoformat())

    with patch("stripe.checkout.Session.create") as create_session:
        with pytest.raises(ValidationError):
            create_subscription_checkout_session(
                organizer=organizer,
                tier_price=tier_price,
                user=user,
            )
        with pytest.raises(ValidationError):
            create_addon_checkout_session(
                organizer=organizer,
                addon=recurring_addon,
                user=user,
            )

    create_session.assert_not_called()


@pytest.mark.django_db
def test_post_cutover_subscription_webhooks_cancel_legacy_subscriptions(setup_data):
    organizer, event, user, _, _, tier_price, recurring_addon, one_time_addon, _ = (
        setup_data
    )
    organizer.settings.set(BILLING_CUTOVER_SETTING, now().isoformat())
    billing = OrganizerBillingModel.objects.create(
        organizer=organizer,
        stripe_customer_id="cus_cutover",
        stripe_payment_method_id="pm_newer",
    )
    authorization = BillingCollectionAuthorization.objects.create(
        organizer=organizer,
        stripe_customer_id="cus_cutover",
        authorized_at=now(),
        revoked_at=now(),
    )
    pending_addon = OrganizerAddon.objects.create(
        organizer=organizer,
        addon=recurring_addon,
        status=AddonStatus.PENDING,
        quantity=1,
    )
    plan_session = {
        "subscription": "sub_late_plan",
        "metadata": {
            "type": "subscription",
            "organizer_slug": organizer.slug,
            "tier_price_id": str(tier_price.pk),
            "tier_version_id": str(tier_price.tier_version_id),
            "user_id": str(user.pk),
        },
    }
    addon_session = {
        "subscription": "sub_late_addon",
        "metadata": {
            "type": "addon_purchase",
            "scope": "organizer",
            "organizer_slug": organizer.slug,
            "addon_id": str(recurring_addon.pk),
            "quantity": "1",
            "assignment_id": str(pending_addon.pk),
            "user_id": str(user.pk),
        },
    }
    pending_one_time_addon = EventAddon.objects.create(
        event=event,
        addon=one_time_addon,
        status=AddonStatus.PENDING,
        quantity=1,
    )
    payment_session = {
        "customer": "cus_cutover",
        "payment_intent": "pi_late_one_time",
        "metadata": {
            "type": "addon_purchase",
            "scope": "event",
            "organizer_slug": organizer.slug,
            "event_slug": event.slug,
            "addon_id": str(one_time_addon.pk),
            "quantity": "1",
            "assignment_id": str(pending_one_time_addon.pk),
            "user_id": str(user.pk),
        },
    }

    with patch(
        "eventyay_business.stripe_service.get_stripe_secret_key_safe",
        return_value="sk_test_cutover",
    ), patch("stripe.Subscription.cancel") as cancel_subscription:
        assert process_webhook_event("checkout.session.completed", plan_session) is None
        assert (
            process_webhook_event("checkout.session.completed", addon_session) is None
        )
        processed_one_time_addon = process_webhook_event(
            "checkout.session.completed", payment_session
        )

    assert cancel_subscription.call_args_list == [
        call("sub_late_plan"),
        call("sub_late_addon"),
    ]
    assert not OrganizerAddon.objects.filter(pk=pending_addon.pk).exists()
    assert processed_one_time_addon.pk == pending_one_time_addon.pk
    assert processed_one_time_addon.status == AddonStatus.ACTIVE
    billing.refresh_from_db()
    authorization.refresh_from_db()
    assert billing.stripe_payment_method_id == "pm_newer"
    assert authorization.revoked_at is not None


@pytest.mark.django_db
def test_webhook_process_addon_checkout_completed(setup_data):
    organizer, event, user, _, _, _, recurring_addon, one_time_event_addon, _ = (
        setup_data
    )

    billing = OrganizerBillingModel.objects.create(
        organizer=organizer,
        stripe_customer_id="cus_addon_completed",
    )
    stripe_client = MagicMock()
    stripe_client.payment_intents.retrieve.return_value = MagicMock(
        payment_method=MagicMock(id="pm_addon_completed")
    )

    signal_mock = MagicMock()
    addon_purchased.connect(signal_mock)
    try:
        # 1. Organizer Addon purchase
        session_data_org = {
            "customer": "cus_addon_completed",
            "subscription": "sub_stripe_111",
            "payment_intent": "pi_stripe_111",
            "metadata": {
                "type": "addon_purchase",
                "organizer_slug": organizer.slug,
                "addon_id": str(recurring_addon.pk),
                "quantity": "3",
                "user_id": str(user.pk),
            },
        }
        with patch(
            "eventyay_business.stripe_service.get_stripe_secret_key_safe",
            return_value="sk_test_automatic",
        ), patch("stripe.StripeClient", return_value=stripe_client):
            res = process_webhook_event("checkout.session.completed", session_data_org)
        assert isinstance(res, OrganizerAddon)
        assert res.status == AddonStatus.ACTIVE
        assert res.quantity == 3
        assert res.price == Decimal("25.00")
        assert res.stripe_subscription_id == "sub_stripe_111"
        assert res.ends_at is not None
        assert signal_mock.call_count == 1
        authorization = BillingCollectionAuthorization.objects.get(organizer=organizer)
        authorization.revoked_at = now()
        authorization.save(update_fields=["revoked_at"])
        billing.stripe_payment_method_id = "pm_newer"
        billing.save(update_fields=["stripe_payment_method_id"])

        # Idempotent replay does not create duplicate
        res_repeat = process_webhook_event(
            "checkout.session.completed", session_data_org
        )
        assert res_repeat.pk == res.pk
        assert OrganizerAddon.objects.filter(organizer=organizer).count() == 1
        assert signal_mock.call_count == 1  # No second signal
        authorization.refresh_from_db()
        billing.refresh_from_db()
        assert authorization.revoked_at is not None
        assert billing.stripe_payment_method_id == "pm_newer"

        # 2. Event Addon purchase with existing pending assignment
        pending_ea = EventAddon.objects.create(
            event=event,
            addon=one_time_event_addon,
            status=AddonStatus.PENDING,
            quantity=1,
        )
        session_data_event = {
            "payment_intent": "pi_stripe_222",
            "metadata": {
                "type": "addon_purchase",
                "organizer_slug": organizer.slug,
                "event_slug": event.slug,
                "addon_id": str(one_time_event_addon.pk),
                "assignment_id": str(pending_ea.pk),
                "quantity": "1",
                "user_id": str(user.pk),
            },
        }
        with patch(
            "eventyay_business.stripe_service.sync_checkout_payment_method",
            return_value=None,
        ), patch(
            "eventyay_business.stripe_service.record_automatic_collection_authorization"
        ) as record_authorization:
            res_ea = process_webhook_event(
                "checkout.session.completed", session_data_event
            )
        record_authorization.assert_not_called()
        assert res_ea.pk == pending_ea.pk
        assert res_ea.status == AddonStatus.ACTIVE
        assert res_ea.stripe_payment_intent_id == "pi_stripe_222"
        assert res_ea.ends_at is None  # One-time
        assert signal_mock.call_count == 2
    finally:
        addon_purchased.disconnect(signal_mock)


@pytest.mark.django_db
def test_webhook_process_subscription_checkout_completed(setup_data):
    organizer, _, user, _, tier_version, tier_price, _, _, _ = setup_data
    OrganizerBillingModel.objects.create(
        organizer=organizer,
        primary_contact_name="Stripe Billing",
        primary_contact_email="billing@example.com",
        company_or_organization_name="Stripe Org",
        address_line_1="Example Street 1",
        address_line_2="",
        city="Berlin",
        zip_code="10115",
        country="DE",
        preferred_language="en",
        tax_id="",
        stripe_customer_id="cus_sub_completed",
    )

    signal_mock = MagicMock()
    subscription_purchased.connect(signal_mock)
    try:
        session_data = {
            "customer": "cus_sub_completed",
            "subscription": "sub_live_completed",
            "invoice": "in_sub_completed",
            "metadata": {
                "type": "subscription",
                "organizer_slug": organizer.slug,
                "tier_price_id": str(tier_price.pk),
                "tier_version_id": str(tier_version.pk),
                "user_id": str(user.pk),
            },
        }

        stripe_client = MagicMock()
        stripe_client.subscriptions.retrieve.return_value = MagicMock(
            default_payment_method=None,
            latest_invoice={
                "payment_intent": {
                    "payment_method": {"id": "pm_sub_completed"},
                }
            },
        )
        with patch(
            "eventyay_business.stripe_service.get_stripe_secret_key_safe",
            return_value="sk_test_automatic",
        ), patch("stripe.StripeClient", return_value=stripe_client):
            sub = process_webhook_event("checkout.session.completed", session_data)
        assert isinstance(sub, Subscription)
        assert sub.status == SubscriptionStatus.ACTIVE
        assert sub.tier_version == tier_version
        assert sub.stripe_subscription_id == "sub_live_completed"
        assert sub.stripe_customer_id == "cus_sub_completed"
        assert signal_mock.call_count == 1
        authorization = BillingCollectionAuthorization.objects.get(organizer=organizer)
        assert authorization.authorized_by == user
        assert authorization.terms_version == "2"
        billing = OrganizerBillingModel.objects.get(organizer=organizer)
        assert billing.stripe_payment_method_id == "pm_sub_completed"
        authorization.revoked_at = now()
        authorization.save(update_fields=["revoked_at"])
        billing.stripe_payment_method_id = "pm_newer"
        billing.save(update_fields=["stripe_payment_method_id"])

        # Idempotent
        with patch(
            "eventyay_business.stripe_service.get_stripe_secret_key_safe",
            return_value=None,
        ):
            sub_dup = process_webhook_event("checkout.session.completed", session_data)
        assert sub_dup.pk == sub.pk
        assert signal_mock.call_count == 1
        authorization.refresh_from_db()
        billing.refresh_from_db()
        assert authorization.revoked_at is not None
        assert billing.stripe_payment_method_id == "pm_newer"

        session_without_payment_method = {
            **session_data,
            "subscription": "sub_without_payment_method",
            "invoice": "in_without_payment_method",
        }
        with patch(
            "eventyay_business.stripe_service.sync_checkout_payment_method",
            return_value=None,
        ), patch(
            "eventyay_business.stripe_service.record_automatic_collection_authorization"
        ) as record_authorization, patch(
            "eventyay_business.stripe_service.get_stripe_secret_key_safe",
            return_value=None,
        ):
            process_webhook_event(
                "checkout.session.completed", session_without_payment_method
            )
        record_authorization.assert_not_called()
        authorization.refresh_from_db()
        billing.refresh_from_db()
        assert authorization.revoked_at is not None
        assert billing.stripe_payment_method_id == "pm_newer"
    finally:
        subscription_purchased.disconnect(signal_mock)


@pytest.mark.django_db
def test_webhook_subscription_lifecycle_events(setup_data):
    organizer, _, _, _, tier_version, _, recurring_addon, _, _ = setup_data

    organizer.subscriptions.all().delete()
    sub = Subscription.objects.create(
        organizer=organizer,
        tier_version=tier_version,
        status=SubscriptionStatus.ACTIVE,
        starts_at=now(),
        stripe_subscription_id="sub_test_lifecycle",
    )
    addon = OrganizerAddon.objects.create(
        organizer=organizer,
        addon=recurring_addon,
        status=AddonStatus.ACTIVE,
        stripe_subscription_id="sub_test_lifecycle",
    )

    # 1. customer.subscription.updated with past_due
    process_webhook_event(
        "customer.subscription.updated",
        {"id": "sub_test_lifecycle", "status": "past_due"},
    )
    sub.refresh_from_db()
    assert sub.status == SubscriptionStatus.PAST_DUE

    # 2. invoice.payment_failed marks past_due
    sub.status = SubscriptionStatus.ACTIVE
    sub.save()
    process_webhook_event(
        "invoice.payment_failed",
        {"subscription": "sub_test_lifecycle"},
    )
    sub.refresh_from_db()
    assert sub.status == SubscriptionStatus.PAST_DUE

    # 3. invoice.paid restores active
    process_webhook_event(
        "invoice.paid",
        {"subscription": "sub_test_lifecycle"},
    )
    sub.refresh_from_db()
    assert sub.status == SubscriptionStatus.ACTIVE

    # 4. customer.subscription.deleted cancels both subscription and addon
    process_webhook_event(
        "customer.subscription.deleted",
        {"id": "sub_test_lifecycle", "status": "canceled"},
    )
    sub.refresh_from_db()
    addon.refresh_from_db()
    assert sub.status == SubscriptionStatus.CANCELED
    assert addon.status == AddonStatus.CANCELED


@pytest.mark.django_db
def test_stripe_webhook_view_http(setup_data):
    rf = RequestFactory()

    # Missing signature header -> 400
    req = rf.post(
        "/control/business/stripe/webhook/", data=b"{}", content_type="application/json"
    )
    resp = stripe_business_webhook_view(req)
    assert resp.status_code == 400

    # Missing secret key in settings -> 503
    with patch(
        "eventyay.helpers.stripe_utils.get_stripe_webhook_secret_key",
        side_effect=Exception("No key"),
    ):
        req = rf.post(
            "/control/business/stripe/webhook/",
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="sig_123",
        )
        resp = stripe_business_webhook_view(req)
        assert resp.status_code == 503

    # Valid signature processes event -> 200
    with patch(
        "eventyay.helpers.stripe_utils.get_stripe_webhook_secret_key",
        return_value="whsec_test_secret",
    ):
        with patch("stripe.Webhook.construct_event") as mock_construct:
            fake_event = MagicMock()
            fake_event.type = "checkout.session.completed"
            fake_event.data.object = {"metadata": {}}
            mock_construct.return_value = fake_event

            req = rf.post(
                "/control/business/stripe/webhook/",
                data=b'{"id": "evt_test"}',
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE="sig_valid_123",
            )
            resp = stripe_business_webhook_view(req)
            assert resp.status_code == 200


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_addon_purchase_views_stripe_redirect(admin_client, admin_user, setup_data):
    organizer, event, _, _, _, _, recurring_addon, one_time_event_addon, free_addon = (
        setup_data
    )

    # Give admin_user permissions
    session = admin_client.session
    session.save()
    StaffSession.objects.create(
        user=admin_user,
        session_key=session.session_key,
        comment="test",
    )
    admin_user.is_staff = True
    admin_user.save()

    org_purchase_url = reverse(
        "plugins:eventyay_business:organizer.addon.purchase",
        kwargs={"organizer": organizer.slug, "pk": recurring_addon.pk},
    )
    resp = admin_client.get(org_purchase_url)
    assert resp.status_code == 200
    assert b"automatically charge future organizer invoices" in resp.content

    # 1. Free add-on activates immediately without Stripe redirect
    free_url = reverse(
        "plugins:eventyay_business:organizer.addon.purchase",
        kwargs={"organizer": organizer.slug, "pk": free_addon.pk},
    )
    resp = admin_client.post(free_url, {"quantity": 1})
    assert resp.status_code == 302
    assert "https://checkout.stripe.com" not in resp.url
    assert OrganizerAddon.objects.filter(
        organizer=organizer, addon=free_addon, status=AddonStatus.ACTIVE
    ).exists()

    # 2. Paid add-on with Stripe configured creates PENDING assignment and redirects to Stripe Checkout
    with patch("eventyay_business.views.is_stripe_configured", return_value=True):
        with patch(
            "eventyay_business.views.create_addon_checkout_session",
            return_value="https://checkout.stripe.com/c/pay/cs_test_redirect",
        ) as mock_create_checkout:
            resp = admin_client.post(org_purchase_url, {"quantity": 2})
            assert resp.status_code == 302
            assert resp.url == "https://checkout.stripe.com/c/pay/cs_test_redirect"
            mock_create_checkout.assert_called_once()
            pending = OrganizerAddon.objects.filter(
                organizer=organizer, addon=recurring_addon, status=AddonStatus.PENDING
            ).first()
            assert pending is not None
            assert pending.quantity == 2

    # 3. Event-level paid add-on purchase with Stripe configured
    event_purchase_url = reverse(
        "plugins:eventyay_business:event.addon.purchase",
        kwargs={
            "organizer": organizer.slug,
            "event": event.slug,
            "pk": one_time_event_addon.pk,
        },
    )
    with patch("eventyay_business.views.is_stripe_configured", return_value=True):
        with patch(
            "eventyay_business.views.create_addon_checkout_session",
            return_value="https://checkout.stripe.com/c/pay/cs_event_test_redirect",
        ) as mock_create_checkout:
            resp = admin_client.post(event_purchase_url, {"quantity": 1})
            assert resp.status_code == 302
            assert (
                resp.url == "https://checkout.stripe.com/c/pay/cs_event_test_redirect"
            )
            mock_create_checkout.assert_called_once()
            pending_ea = EventAddon.objects.filter(
                event=event, addon=one_time_event_addon, status=AddonStatus.PENDING
            ).first()
            assert pending_ea is not None


@pytest.mark.django_db
def test_sync_tier_price_to_stripe(setup_data):
    _, _, _, tier, tier_version, tier_price, _, _, _ = setup_data

    with patch(
        "eventyay_business.stripe_service.get_stripe_secret_key_safe",
        return_value="sk_test_123",
    ):
        with patch("stripe.Product.create") as mock_prod, patch(
            "stripe.Price.create"
        ) as mock_price:
            mock_prod.return_value = MagicMock(id="prod_tier_test_123")
            mock_price.return_value = MagicMock(id="price_tier_test_123")

            price_id = sync_tier_price_to_stripe(tier_price)
            assert price_id == "price_tier_test_123"

            tier.refresh_from_db()
            tier_price.refresh_from_db()
            assert tier.stripe_product_id == "prod_tier_test_123"
            assert tier_price.stripe_price_id == "price_tier_test_123"

            # Re-calling returns existing price_id without recreating
            mock_price.reset_mock()
            mock_prod.reset_mock()
            assert sync_tier_price_to_stripe(tier_price) == "price_tier_test_123"
            mock_price.assert_not_called()


@pytest.mark.django_db
def test_sync_addon_to_stripe(setup_data):
    _, _, _, _, _, _, recurring_addon, _, _ = setup_data

    with patch(
        "eventyay_business.stripe_service.get_stripe_secret_key_safe",
        return_value="sk_test_123",
    ):
        with patch("stripe.Product.create") as mock_prod, patch(
            "stripe.Price.create"
        ) as mock_price:
            mock_prod.return_value = MagicMock(id="prod_addon_test_123")
            mock_price.return_value = MagicMock(id="price_addon_test_123")

            price_id = sync_addon_to_stripe(recurring_addon)
            assert price_id == "price_addon_test_123"

            recurring_addon.refresh_from_db()
            assert recurring_addon.stripe_product_id == "prod_addon_test_123"
            assert recurring_addon.stripe_price_id == "price_addon_test_123"


@pytest.mark.django_db
def test_invoice_payment_failed_and_paid_updates_linked_addons(setup_data):
    (
        organizer,
        event,
        _,
        _,
        tier_version,
        _,
        recurring_addon,
        one_time_event_addon,
        _,
    ) = setup_data
    organizer.subscriptions.all().delete()

    sub = Subscription.objects.create(
        organizer=organizer,
        tier_version=tier_version,
        status=SubscriptionStatus.ACTIVE,
        starts_at=now(),
        stripe_subscription_id="sub_test_shared_invoice",
    )
    org_addon = OrganizerAddon.objects.create(
        organizer=organizer,
        addon=recurring_addon,
        status=AddonStatus.ACTIVE,
        stripe_subscription_id="sub_test_shared_invoice",
    )
    ea_addon = EventAddon.objects.create(
        event=event,
        addon=one_time_event_addon,
        status=AddonStatus.ACTIVE,
        stripe_subscription_id="sub_test_shared_invoice",
    )

    # 1. invoice.payment_failed marks both subscription and linked add-ons PAST_DUE
    process_webhook_event(
        "invoice.payment_failed", {"subscription": "sub_test_shared_invoice"}
    )
    sub.refresh_from_db()
    org_addon.refresh_from_db()
    ea_addon.refresh_from_db()

    assert sub.status == SubscriptionStatus.PAST_DUE
    assert org_addon.status == AddonStatus.PAST_DUE
    assert ea_addon.status == AddonStatus.PAST_DUE

    # 2. invoice.paid restores both subscription and linked add-ons back to ACTIVE
    process_webhook_event("invoice.paid", {"subscription": "sub_test_shared_invoice"})
    sub.refresh_from_db()
    org_addon.refresh_from_db()
    ea_addon.refresh_from_db()

    assert sub.status == SubscriptionStatus.ACTIVE
    assert org_addon.status == AddonStatus.ACTIVE
    assert ea_addon.status == AddonStatus.ACTIVE


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_addon_purchase_views_stripe_error_cleanup(
    setup_data, admin_user, admin_client
):
    organizer, _, _, _, _, _, recurring_addon, _, _ = setup_data

    session = admin_client.session
    session.save()
    StaffSession.objects.create(
        user=admin_user,
        session_key=session.session_key,
        comment="test",
    )
    admin_user.is_staff = True
    admin_user.save()

    purchase_url = reverse(
        "plugins:eventyay_business:organizer.addon.purchase",
        kwargs={"organizer": organizer.slug, "pk": recurring_addon.pk},
    )

    # When Stripe raises an API or network exception
    with patch("eventyay_business.views.is_stripe_configured", return_value=True):
        with patch(
            "eventyay_business.views.create_addon_checkout_session",
            side_effect=Exception("Stripe API connection timeout"),
        ):
            resp = admin_client.post(purchase_url, {"quantity": 1})
            # Redirects back to plan with error message
            assert resp.status_code == 302
            assert (
                reverse(
                    "plugins:eventyay_business:organizer.plan",
                    kwargs={"organizer": organizer.slug},
                )
                in resp.url
            )
            # Assert pending assignment was cleaned up / deleted
            assert not OrganizerAddon.objects.filter(
                organizer=organizer, addon=recurring_addon, status=AddonStatus.PENDING
            ).exists()


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_organizer_plan_upgrade_view_flow(setup_data, admin_user, admin_client):
    organizer, _, _, tier, tier_version, tier_price, _, _, _ = setup_data

    session = admin_client.session
    session.save()
    StaffSession.objects.create(
        user=admin_user,
        session_key=session.session_key,
        comment="test",
    )
    admin_user.is_staff = True
    admin_user.save()

    upgrade_url = reverse(
        "plugins:eventyay_business:organizer.plan.upgrade",
        kwargs={"organizer": organizer.slug},
    )

    tier.is_public = True
    tier.save()
    tier_version.published_at = now()
    tier_version.save()

    # 1. GET upgrade view
    resp = admin_client.get(upgrade_url)
    assert resp.status_code == 200
    assert tier.name.encode() in resp.content
    assert b"automatically charge future organizer invoices" in resp.content

    # Create a free tier plan
    free_tier = Tier.objects.create(
        name="Free Community",
        slug="free-community",
        status=TierStatus.PUBLISHED,
        is_public=True,
    )
    free_version = TierVersion.objects.create(
        tier=free_tier, version=1, published_at=now()
    )
    free_price = TierPrice.objects.create(
        tier_version=free_version,
        billing_interval=BillingInterval.MONTHLY,
        amount=Decimal("0.00"),
        currency="USD",
        active=True,
    )

    # 2. POST with free tier: switches subscription immediately
    resp = admin_client.post(upgrade_url, {"tier_price_id": free_price.id})
    assert resp.status_code == 302
    assert (
        reverse(
            "plugins:eventyay_business:organizer.plan",
            kwargs={"organizer": organizer.slug},
        )
        in resp.url
    )

    active_sub = Subscription.objects.filter(
        organizer=organizer, status=SubscriptionStatus.ACTIVE
    ).first()
    assert active_sub is not None
    assert active_sub.tier_version == free_version

    # 3. POST with paid tier when Stripe configured: redirects to Stripe Checkout
    tier.is_public = True
    tier.save()
    tier_version.published_at = now()
    tier_version.save()

    with patch("eventyay_business.views.is_stripe_configured", return_value=True):
        with patch(
            "eventyay_business.views.create_subscription_checkout_session",
            return_value="https://checkout.stripe.com/c/pay/cs_tier_upgrade_123",
        ) as mock_checkout:
            resp = admin_client.post(upgrade_url, {"tier_price_id": tier_price.id})
            assert resp.status_code == 302
            assert resp.url == "https://checkout.stripe.com/c/pay/cs_tier_upgrade_123"
            mock_checkout.assert_called_once()

    organizer.settings.set(BILLING_CUTOVER_SETTING, now().isoformat())
    with patch(
        "eventyay_business.views.is_stripe_configured", return_value=True
    ), patch(
        "eventyay_business.views.create_subscription_checkout_session"
    ) as mock_checkout:
        resp = admin_client.post(upgrade_url, {"tier_price_id": tier_price.id})

    assert resp.status_code == 302
    mock_checkout.assert_not_called()
    active_sub.refresh_from_db()
    assert active_sub.tier_version == tier_version


@pytest.mark.django_db
def test_stripe_checkout_cancel_view_cleanup(setup_data):
    organizer, event, user, _, _, _, recurring_addon, one_time_event_addon, _ = (
        setup_data
    )

    rf = RequestFactory()

    from django.contrib.messages.storage.fallback import FallbackStorage
    from django.contrib.sessions.backends.db import SessionStore

    # 1. Organizer-scoped pending addon deleted on cancel
    org_pending = OrganizerAddon.objects.create(
        organizer=organizer,
        addon=recurring_addon,
        status=AddonStatus.PENDING,
        quantity=1,
    )
    req = rf.get(
        reverse(
            "plugins:eventyay_business:checkout.cancel",
            kwargs={"organizer": organizer.slug},
        )
        + f"?assignment_id={org_pending.pk}&scope=organizer"
    )
    req.user = user
    req.session = SessionStore()
    setattr(req, "_messages", FallbackStorage(req))
    resp = StripeCheckoutCancelView.as_view()(req, organizer=organizer.slug)
    assert resp.status_code == 302
    assert not OrganizerAddon.objects.filter(pk=org_pending.pk).exists()

    # 2. Event-scoped pending addon deleted on cancel
    event_pending = EventAddon.objects.create(
        event=event,
        addon=one_time_event_addon,
        status=AddonStatus.PENDING,
        quantity=1,
    )
    req_event = rf.get(
        reverse(
            "plugins:eventyay_business:event.checkout.cancel",
            kwargs={"organizer": organizer.slug, "event": event.slug},
        )
        + f"?assignment_id={event_pending.pk}&scope=event"
    )
    req_event.user = user
    req_event.session = SessionStore()
    setattr(req_event, "_messages", FallbackStorage(req_event))
    resp_event = StripeCheckoutCancelView.as_view()(
        req_event, organizer=organizer.slug, event=event.slug
    )
    assert resp_event.status_code == 302
    assert not EventAddon.objects.filter(pk=event_pending.pk).exists()


@pytest.mark.django_db
def test_stripe_api_2025_03_31_compatibility(setup_data):
    organizer, _, _, _, _, _, _, _, _ = setup_data
    sub = organizer.subscriptions.filter(status=SubscriptionStatus.ACTIVE).first()
    if not sub:
        sub = Subscription.objects.create(
            organizer=organizer,
            tier_version=setup_data[4],
            status=SubscriptionStatus.ACTIVE,
            starts_at=now(),
        )
    sub.stripe_subscription_id = "sub_test_2025"
    sub.save()

    # 1. Subscription change with current_period_end inside items.data
    future_ts = int((now() + timedelta(days=60)).timestamp())
    sub_data = {
        "id": "sub_test_2025",
        "status": "active",
        "items": {"data": [{"current_period_end": future_ts}]},
    }
    process_subscription_change("customer.subscription.updated", sub_data)
    sub.refresh_from_db()
    assert sub.ends_at is not None
    assert int(sub.ends_at.timestamp()) == future_ts

    # 2. Invoice failed with subscription inside parent.subscription_details
    invoice_failed_data = {
        "parent": {
            "type": "subscription_details",
            "subscription_details": {"subscription": "sub_test_2025"},
        }
    }
    process_invoice_payment_failed(invoice_failed_data)
    sub.refresh_from_db()
    assert sub.status == SubscriptionStatus.PAST_DUE

    # 3. Invoice paid with subscription inside parent.subscription_details
    invoice_paid_data = {
        "parent": {
            "type": "subscription_details",
            "subscription_details": {"subscription": "sub_test_2025"},
        }
    }
    process_invoice_paid(invoice_paid_data)
    sub.refresh_from_db()
    assert sub.status == SubscriptionStatus.ACTIVE


@pytest.mark.django_db
def test_subscription_checkout_creates_business_invoice(setup_data):
    organizer, _, user, tier, tier_version, tier_price, _, _, _ = setup_data

    session_data = {
        "id": "cs_test_invoice_sub",
        "customer": "cus_test_123",
        "subscription": "sub_test_inv_456",
        "payment_intent": "pi_test_inv_789",
        "invoice": "in_test_inv_001",
        "amount_total": 9900,
        "currency": "usd",
        "payment_status": "paid",
        "metadata": {
            "type": "subscription",
            "organizer_slug": organizer.slug,
            "tier_price_id": str(tier_price.pk),
            "tier_version_id": str(tier_version.pk),
            "user_id": str(user.pk),
        },
    }

    sub = process_webhook_event("checkout.session.completed", session_data)
    assert sub is not None
    assert sub.status == SubscriptionStatus.ACTIVE
    assert sub.tier_version == tier_version

    # Check BusinessInvoice creation
    invoices = BusinessInvoice.objects.filter(organizer=organizer)
    assert invoices.count() == 1
    invoice = invoices.first()
    assert invoice.status == BusinessInvoiceStatus.PAID
    assert invoice.total == Decimal("99.00")
    assert invoice.currency == "USD"
    assert invoice.stripe_payment_intent_id == "pi_test_inv_789"
    assert invoice.stripe_invoice_id == "in_test_inv_001"

    # Check line item
    assert invoice.lines.count() == 1
    line = invoice.lines.first()
    assert line.line_type == InvoiceLineType.SUBSCRIPTION
    assert line.amount == Decimal("99.00")
    assert line.tier_version == tier_version

    # Verify idempotency: second processing does not create duplicate invoice
    sub_again = process_webhook_event("checkout.session.completed", session_data)
    assert sub_again.pk == sub.pk
    assert BusinessInvoice.objects.filter(organizer=organizer).count() == 1


@pytest.mark.django_db
def test_addon_checkout_creates_business_invoice(setup_data):
    organizer, _, user, _, _, _, recurring_addon, _, _ = setup_data

    session_data = {
        "id": "cs_test_addon_inv",
        "customer": "cus_test_123",
        "subscription": "sub_test_addon_sub",
        "payment_intent": "pi_test_addon_pi",
        "invoice": "in_test_addon_inv",
        "amount_total": 2500,
        "currency": "usd",
        "payment_status": "paid",
        "metadata": {
            "type": "addon_purchase",
            "scope": recurring_addon.assignment_scope,
            "organizer_slug": organizer.slug,
            "addon_id": str(recurring_addon.pk),
            "quantity": "1",
            "user_id": str(user.pk),
        },
    }

    assignment = process_webhook_event("checkout.session.completed", session_data)
    assert assignment is not None
    assert assignment.status == AddonStatus.ACTIVE

    # Check BusinessInvoice creation
    invoices = BusinessInvoice.objects.filter(organizer=organizer)
    assert invoices.count() == 1
    invoice = invoices.first()
    assert invoice.status == BusinessInvoiceStatus.PAID
    assert invoice.total == Decimal("25.00")
    assert invoice.currency == "USD"

    # Check line item
    assert invoice.lines.count() == 1
    line = invoice.lines.first()
    assert line.line_type == InvoiceLineType.ADDON
    assert line.amount == Decimal("25.00")
    assert line.addon == recurring_addon

    # Idempotency
    process_webhook_event("checkout.session.completed", session_data)
    assert BusinessInvoice.objects.filter(organizer=organizer).count() == 1


@pytest.mark.django_db
def test_stripe_checkout_success_view_synchronous_fulfillment(setup_data):
    organizer, _, user, _, tier_version, tier_price, _, _, _ = setup_data

    # Initially on a different tier or free
    sub = organizer.subscriptions.first()
    assert sub is None or sub.tier_version != tier_version

    mock_session = MagicMock()
    mock_session.id = "cs_sync_test_999"
    mock_session.payment_status = "paid"
    mock_session.customer = "cus_sync_123"
    mock_session.subscription = "sub_sync_456"
    mock_session.payment_intent = "pi_sync_789"
    mock_session.invoice = "in_sync_001"
    mock_session.amount_total = 9900
    mock_session.currency = "usd"
    mock_session.metadata = {
        "type": "subscription",
        "organizer_slug": organizer.slug,
        "tier_price_id": str(tier_price.pk),
        "tier_version_id": str(tier_version.pk),
        "user_id": str(user.pk),
    }
    mock_session.to_dict.return_value = {
        "id": "cs_sync_test_999",
        "payment_status": "paid",
        "customer": "cus_sync_123",
        "subscription": "sub_sync_456",
        "payment_intent": "pi_sync_789",
        "invoice": "in_sync_001",
        "amount_total": 9900,
        "currency": "usd",
        "metadata": mock_session.metadata,
    }

    rf = RequestFactory()
    from django.contrib.messages.storage.fallback import FallbackStorage
    from django.contrib.sessions.backends.db import SessionStore

    req = rf.get(
        reverse(
            "plugins:eventyay_business:checkout.success",
            kwargs={"organizer": organizer.slug},
        )
        + "?session_id=cs_sync_test_999"
    )
    req.user = user
    req.session = SessionStore()
    setattr(req, "_messages", FallbackStorage(req))

    with patch(
        "eventyay_business.stripe_service.get_stripe_secret_key_safe",
        return_value="sk_test_123",
    ):
        with patch("stripe.checkout.Session.retrieve", return_value=mock_session):
            resp = StripeCheckoutSuccessView.as_view()(req, organizer=organizer.slug)
            assert resp.status_code == 302

    # Verify subscription switched immediately
    active_sub = organizer.subscriptions.filter(
        status=SubscriptionStatus.ACTIVE
    ).first()
    assert active_sub is not None
    assert active_sub.tier_version == tier_version

    # Verify invoice was created immediately
    invoice = BusinessInvoice.objects.filter(organizer=organizer).first()
    assert invoice is not None
    assert invoice.status == BusinessInvoiceStatus.PAID
    assert invoice.total == Decimal("99.00")


@pytest.mark.django_db
def test_sync_organizer_from_stripe(setup_data):
    organizer, _, _, _, tier_version, tier_price, _, _, _ = setup_data

    mock_sub = MagicMock()
    mock_sub.id = "sub_remote_live_1"
    mock_sub.latest_invoice = "in_remote_001"
    mock_sub.to_dict.return_value = {
        "id": "sub_remote_live_1",
        "metadata": {
            "tier_price_id": str(tier_price.pk),
            "tier_version_id": str(tier_version.pk),
        },
        "latest_invoice": "in_remote_001",
        "items": {"data": []},
    }

    mock_subs_list = MagicMock()
    mock_subs_list.data = [mock_sub]

    with patch(
        "eventyay_business.stripe_service.get_stripe_secret_key_safe",
        return_value="sk_test_123",
    ):
        with patch(
            "stripe.Customer.search",
            return_value=MagicMock(data=[MagicMock(id="cus_found_1")]),
        ):
            with patch("stripe.Subscription.list", return_value=mock_subs_list):
                synced = sync_organizer_from_stripe(organizer)
                assert synced is not None
                assert synced.status == SubscriptionStatus.ACTIVE
                assert synced.tier_version == tier_version

    # Invoice generated
    inv = BusinessInvoice.objects.filter(organizer=organizer).first()
    assert inv is not None
    assert inv.status == BusinessInvoiceStatus.PAID
    assert inv.total == Decimal("99.00")
