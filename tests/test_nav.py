from django.test import RequestFactory
from django.urls import ResolverMatch
from unittest.mock import Mock

from eventyay_business.signals import (
    business_event_addons_nav,
    business_tiers_nav,
)


def test_business_tiers_nav_anonymous():
    factory = RequestFactory()
    request = factory.get("/common")
    request.user = Mock(is_authenticated=False, is_staff=False, is_superuser=False)
    request.resolver_match = ResolverMatch(
        func=lambda r: None,
        args=(),
        kwargs={},
        url_name="dashboard",
        app_names=["eventyay_common"],
        namespaces=["eventyay_common"],
    )

    items = business_tiers_nav(sender=None, request=request)
    assert items == []


def test_business_tiers_nav_non_staff_authenticated():
    factory = RequestFactory()
    request = factory.get("/common")
    request.user = Mock(is_authenticated=True, is_staff=False, is_superuser=False)
    request.resolver_match = ResolverMatch(
        func=lambda r: None,
        args=(),
        kwargs={},
        url_name="dashboard",
        app_names=["eventyay_common"],
        namespaces=["eventyay_common"],
    )

    items = business_tiers_nav(sender=None, request=request)
    assert items == []


def test_business_tiers_nav_staff_on_common_dashboard():
    """Verify that staff visiting public-facing common dashboard do not see Tiers/Subscriptions."""
    factory = RequestFactory()
    request = factory.get("/common")
    request.user = Mock(is_authenticated=True, is_staff=True, is_superuser=False)
    request.resolver_match = ResolverMatch(
        func=lambda r: None,
        args=(),
        kwargs={},
        url_name="dashboard",
        app_names=["eventyay_common"],
        namespaces=["eventyay_common"],
    )

    items = business_tiers_nav(sender=None, request=request)
    assert items == []


def test_business_tiers_nav_staff_on_organizer_plan():
    """Verify that organizer plan route does not show global business tiers nav."""
    factory = RequestFactory()
    request = factory.get("/control/organizer/test-org/business/plan/")
    request.user = Mock(is_authenticated=True, is_staff=True, is_superuser=False)
    request.resolver_match = ResolverMatch(
        func=lambda r: None,
        args=(),
        kwargs={"organizer": "test-org"},
        url_name="organizer.plan",
        app_names=["plugins:eventyay_business"],
        namespaces=["plugins:eventyay_business"],
    )

    items = business_tiers_nav(sender=None, request=request)
    assert items == []


def test_business_tiers_nav_staff_on_admin_page():
    """Verify that staff on admin routes get Tiers and Subscriptions in navigation."""
    factory = RequestFactory()
    request = factory.get("/admin/global/business/")
    request.user = Mock(is_authenticated=True, is_staff=True, is_superuser=False)
    request.resolver_match = ResolverMatch(
        func=lambda r: None,
        args=(),
        kwargs={},
        url_name="admin.global.business",
        app_names=["eventyay_admin"],
        namespaces=["eventyay_admin"],
    )

    items = business_tiers_nav(sender=None, request=request)
    assert len(items) == 4
    assert str(items[0]["label"]) == "Tiers"
    assert str(items[1]["label"]) == "Subscriptions"
    assert str(items[2]["label"]) == "Add-ons"
    assert str(items[3]["label"]) == "Invoices"
    assert items[0]["active"] is False
    assert items[1]["active"] is False
    assert items[2]["active"] is False
    assert items[3]["active"] is False


def test_business_tiers_nav_staff_on_tiers_list():
    """Verify that staff on tiers list view have Tiers marked active."""
    factory = RequestFactory()
    request = factory.get("/admin/global/business/tiers/")
    request.user = Mock(is_authenticated=True, is_staff=True, is_superuser=False)
    request.resolver_match = ResolverMatch(
        func=lambda r: None,
        args=(),
        kwargs={},
        url_name="tiers.list",
        app_names=["plugins:eventyay_business"],
        namespaces=["plugins:eventyay_business"],
    )

    items = business_tiers_nav(sender=None, request=request)
    assert len(items) == 4
    assert items[0]["active"] is True
    assert items[1]["active"] is False
    assert items[2]["active"] is False
    assert items[3]["active"] is False


def test_business_tiers_nav_staff_on_addons_list():
    """Verify that staff on addons list view have Add-ons marked active."""
    factory = RequestFactory()
    request = factory.get("/admin/global/business/addons/")
    request.user = Mock(is_authenticated=True, is_staff=True, is_superuser=False)
    request.resolver_match = ResolverMatch(
        func=lambda r: None,
        args=(),
        kwargs={},
        url_name="addons.list",
        app_names=["plugins:eventyay_business"],
        namespaces=["plugins:eventyay_business"],
    )

    items = business_tiers_nav(sender=None, request=request)
    assert len(items) == 4
    assert items[0]["active"] is False
    assert items[1]["active"] is False
    assert items[2]["active"] is True
    assert items[3]["active"] is False


def test_business_tiers_nav_staff_on_invoices_list():
    """Verify that staff on invoices list view have Invoices marked active."""
    factory = RequestFactory()
    request = factory.get("/admin/global/business/invoices/")
    request.user = Mock(is_authenticated=True, is_staff=True, is_superuser=False)
    request.resolver_match = ResolverMatch(
        func=lambda r: None,
        args=(),
        kwargs={},
        url_name="invoices.list",
        app_names=["plugins:eventyay_business"],
        namespaces=["plugins:eventyay_business"],
    )

    items = business_tiers_nav(sender=None, request=request)
    assert len(items) == 4
    assert items[0]["active"] is False
    assert items[1]["active"] is False
    assert items[2]["active"] is False
    assert items[3]["active"] is True


def test_business_event_addons_nav_anonymous():
    factory = RequestFactory()
    request = factory.get("/control/event/test-org/test-event/settings/")
    request.user = Mock(is_authenticated=False)
    request.resolver_match = ResolverMatch(
        func=lambda r: None,
        args=(),
        kwargs={"organizer": "test-org", "event": "test-event"},
        url_name="event.settings",
        app_names=["pretixcontrol"],
        namespaces=["control"],
    )
    items = business_event_addons_nav(sender=None, request=request)
    assert items == []


def test_business_event_addons_nav_without_permission():
    factory = RequestFactory()
    request = factory.get("/control/event/test-org/test-event/settings/")
    user = Mock(is_authenticated=True)
    user.has_event_permission.return_value = False
    request.user = user
    request.organizer = Mock(slug="test-org")
    request.event = Mock(slug="test-event")
    request.resolver_match = ResolverMatch(
        func=lambda r: None,
        args=(),
        kwargs={"organizer": "test-org", "event": "test-event"},
        url_name="event.settings",
        app_names=["pretixcontrol"],
        namespaces=["control"],
    )
    items = business_event_addons_nav(sender=request.event, request=request)
    assert items == []


def test_business_event_addons_nav_with_permission():
    factory = RequestFactory()
    request = factory.get("/control/event/test-org/test-event/settings/")
    user = Mock(is_authenticated=True)
    user.has_event_permission.return_value = True
    request.user = user
    request.organizer = Mock(slug="test-org")
    request.event = Mock(slug="test-event")
    request.resolver_match = ResolverMatch(
        func=lambda r: None,
        args=(),
        kwargs={"organizer": "test-org", "event": "test-event"},
        url_name="event.settings",
        app_names=["pretixcontrol"],
        namespaces=["control"],
    )
    items = business_event_addons_nav(sender=request.event, request=request)
    assert len(items) == 1
    assert str(items[0]["label"]) == "Add-ons & Modules"
    assert items[0]["active"] is False
    assert "test-org" in items[0]["url"]
    assert "test-event" in items[0]["url"]


def test_business_event_addons_nav_active():
    factory = RequestFactory()
    request = factory.get("/control/event/test-org/test-event/business/addons/")
    user = Mock(is_authenticated=True)
    user.has_event_permission.return_value = True
    request.user = user
    request.organizer = Mock(slug="test-org")
    request.event = Mock(slug="test-event")
    request.resolver_match = ResolverMatch(
        func=lambda r: None,
        args=(),
        kwargs={"organizer": "test-org", "event": "test-event"},
        url_name="event.addons",
        app_names=["plugins:eventyay_business"],
        namespaces=["plugins:eventyay_business"],
    )
    items = business_event_addons_nav(sender=request.event, request=request)
    assert len(items) == 1
    assert items[0]["active"] is True
