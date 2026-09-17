import logging
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .stripe_service import process_webhook_event

logger = logging.getLogger(__name__)

try:
    import stripe
except ImportError:
    stripe = None


@csrf_exempt
@require_POST
def stripe_business_webhook_view(request):
    """
    Webhook handler for Eventyay Business Stripe billing events:
    - checkout.session.completed
    - customer.subscription.updated / deleted
    - invoice.paid / invoice.payment_failed
    """
    if stripe is None:
        logger.error("Stripe package is not installed")
        return HttpResponse("Stripe unavailable", status=503)

    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")

    if not sig_header:
        logger.error("Missing HTTP_STRIPE_SIGNATURE header in Stripe webhook")
        return HttpResponseBadRequest("Missing signature")

    try:
        from eventyay.helpers.stripe_utils import get_stripe_webhook_secret_key

        webhook_secret = get_stripe_webhook_secret_key()
    except (ValidationError, Exception) as exc:
        logger.exception("Stripe webhook secret is not configured: %s", exc)
        return HttpResponse("Webhook secret not configured", status=503)

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except ValueError as exc:
        logger.error("Invalid payload in Stripe webhook: %s", exc)
        return HttpResponseBadRequest("Invalid payload")
    except stripe.error.SignatureVerificationError as exc:
        logger.error("Invalid signature in Stripe webhook: %s", exc)
        return HttpResponseBadRequest("Invalid signature")

    try:
        process_webhook_event(event.type, event.data.object)
    except Exception as exc:
        logger.exception(
            "Error processing Stripe webhook event %s: %s", event.type, exc
        )
        return HttpResponse("Error processing event", status=500)

    return HttpResponse("Success", status=200)


class StripeCheckoutSuccessView(View):
    def get(self, request, *args, **kwargs):
        organizer = kwargs.get("organizer")
        event = kwargs.get("event")
        session_id = request.GET.get("session_id")

        fulfilled = False
        if session_id:
            from .stripe_service import fulfill_checkout_session_by_id

            try:
                result = fulfill_checkout_session_by_id(session_id)
                if result:
                    fulfilled = True
            except Exception as exc:
                logger.exception(
                    "Error fulfilling checkout session %s on success return: %s",
                    session_id,
                    exc,
                )

        if not fulfilled and organizer:
            from eventyay.base.models import Organizer

            from .stripe_service import sync_organizer_from_stripe

            try:
                org_obj = Organizer.objects.filter(slug=organizer).first()
                if org_obj:
                    sync_result = sync_organizer_from_stripe(org_obj)
                    if sync_result:
                        fulfilled = True
            except Exception as exc:
                logger.warning(
                    "Failed fallback Stripe sync on checkout success: %s", exc
                )

        if fulfilled:
            messages.success(
                request,
                _("Your payment was successful and your plan has been activated!"),
            )
        else:
            messages.success(
                request,
                _(
                    "Your payment was successful! Your subscription or add-on is being "
                    "activated and will appear shortly."
                ),
            )
        if event:
            return redirect(
                "plugins:eventyay_business:event.addons",
                organizer=organizer,
                event=event,
            )
        return redirect("plugins:eventyay_business:organizer.plan", organizer=organizer)


class StripeCheckoutCancelView(View):
    def get(self, request, *args, **kwargs):
        organizer = kwargs.get("organizer")
        event = kwargs.get("event")
        assignment_id = request.GET.get("assignment_id")
        scope = request.GET.get("scope")

        if assignment_id:
            from .models import AddonStatus, EventAddon, OrganizerAddon

            try:
                if scope == "event" or event:
                    assignment = EventAddon.objects.filter(
                        pk=assignment_id, status=AddonStatus.PENDING
                    ).first()
                else:
                    assignment = OrganizerAddon.objects.filter(
                        pk=assignment_id, status=AddonStatus.PENDING
                    ).first()
                if assignment:
                    assignment.delete()
            except Exception as exc:
                logger.warning(
                    "Failed to clean up canceled assignment %s: %s",
                    assignment_id,
                    exc,
                )

        messages.info(
            request,
            _("The checkout process was canceled. No charges were made."),
        )
        if event:
            return redirect(
                "plugins:eventyay_business:event.addons",
                organizer=organizer,
                event=event,
            )
        return redirect("plugins:eventyay_business:organizer.plan", organizer=organizer)
