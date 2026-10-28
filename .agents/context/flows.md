# Business lifecycle and failure paths

## Entitlements and usage

For per-key defaults, evaluation, callers, and gaps, read
[capabilities and entitlements](capabilities-and-entitlements.md).

Core dispatches `entitlement_check`; `signals.py` evaluates subscription/tier and
add-on policy using `capabilities.py` and `services.py`. Follow cache invalidation
when changing assignments or publishing versions. Core's no-receiver default is
allow; Business denial is therefore a deliberate plugin policy.

Core's `record_usage` emits a stable idempotency key and attribution. Business
persists usage through `services.record_usage`. Preserve organizer/event scope,
source identity, and repeat-delivery behavior. Relevant tests include
`tests/test_entitlement_receiver.py`, `test_capabilities.py`,
`test_addon_entitlements.py`, and `test_seed_entitlements.py`.

## Fees, invoices, and collection

1. `services.resolve_fee_settings` selects a country/currency override, otherwise
   `commerce.platform_fee_percent` from a tier version, otherwise zero. With no
   country override, the maximum is zero (no cap). Old core global ticket fee
   settings are not read or automatically migrated; see `README.rst`.
2. Receivers in `signals.py` handle paid/canceled orders and saved refunds.
   `platform_fee_ledger.py` records fees and adjustments, with payment-date and
   billing-cutover checks. It reads historical application fees from payment data.
3. `invoicing_service.py` calculates charges from subscriptions, add-ons, usage,
   and fee ledger sources; it claims sources and finalizes invoice snapshots.
   `tasks.py` provides monthly generation and collection entry points.
4. `invoice_collection.py` validates the core billing customer, payment method,
   and matching unrevoked `BillingCollectionAuthorization`. It tracks collection
   attempts, idempotency, existing PaymentIntents, retry timing, failure codes,
   reconciliation needs, and payment state.
5. Signed Business webhook events reach `process_business_payment_intent` through
   `stripe_service.process_webhook_event`. Notifications and collection-health
   updates follow the resulting state, not merely a browser redirect.

Read `tests/test_platform_fee.py`, `test_fee_policy_provider.py`,
`test_invoices.py`, `test_invoice_collection.py`, and
`test_billing_notifications.py` for expected behavior. Exercise duplicate events,
refunds, currency precision, missing authorization, and interrupted collection
when changing these paths; do not charge real customers to validate them.

## Legacy Checkout and cutover

`stripe_service.py` also creates Checkout sessions and processes tier/add-on
purchases, subscription updates, and paid/failed Stripe invoices.
`views_stripe.py` provides the signed webhook and checkout return handlers;
the success view has retrieval/synchronization fallback behavior. It is not the
only route by which a purchase is fulfilled.

`billing_migration.py` performs consolidated-billing preflight and activation;
`platform_fee_ledger.py` reads `business_billing_cutover_at`.
`reconciliation.py` calculates/accepts evidence for rollout. After cutover,
legacy checkout creation is guarded and late legacy events require careful
handling. Preserve the core legacy-collection disable signal to avoid charging
through two collectors. Read `tests/test_billing_migration.py`,
`test_billing_reconciliation.py`, `test_stripe_integration.py`, and
`test_subscription_lifecycle.py` before changing this transition.

## Routing and scheduled work

`urls.py` maps `/pricing/`, `/admin/global/business/`, organizer/event business
views under `/control/`, and `/control/business/stripe/webhook/`. Use Django URL
names and permission checks from existing views rather than constructing paths.

`apps.py` imports tasks; `tasks.py` defines lifecycle, invoice, collection,
notification, and reconciliation work. Task definition alone does not prove a
production schedule exists: inspect the core Celery/beat configuration and
scheduled tasks for the deployment. Test task functions independently from the
scheduler when possible.

Both Business and the attendee Stripe handler currently obtain the webhook
secret from core's global `stripe_webhook_secret_key`. Preserve signature checks
and inspect actual endpoint configuration; do not assume separate secret fields.
