import pytest
from django.utils.timezone import now
from eventyay.base.models import Event, Organizer

from eventyay_business.forms import (
    SEARCHABLE_SELECT_CLASS,
    BillingNotificationTestForm,
    CountryFeeSettingForm,
    EventAddonForm,
    OrganizerAddonForm,
    SubscriptionAdminForm,
)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("form_class", "field_names"),
    [
        (SubscriptionAdminForm, ("organizer", "tier_version")),
        (OrganizerAddonForm, ("organizer", "addon")),
        (EventAddonForm, ("event", "addon")),
        (BillingNotificationTestForm, ("organizer", "event_type", "locale")),
        (CountryFeeSettingForm, ("country", "currency")),
    ],
)
def test_long_dropdowns_are_searchable(form_class, field_names):
    organizer = Organizer.objects.create(name="Acme", slug="acme")
    Event.objects.create(
        organizer=organizer, name="Acme Conf", slug="conf", date_from=now()
    )

    form = form_class()

    for name in field_names:
        assert form.fields[name].widget.attrs["class"] == SEARCHABLE_SELECT_CLASS
        assert "select2-static" in str(form[name])
