# Capability catalogue, evaluation, and actual enforcement

## Terms and sources

A **capability** is a named, typed definition in
`eventyay_business/capabilities.py`: label, description, category, unit, default,
and audience metadata. A **tier entitlement** is a `TierEntitlement` row in
`models.py`, unique for each combination of tier version and capability, storing a value and optional
overage configuration. An **add-on assignment** grants a boolean feature or
adds integer allowance to the selected organizer/event. A **usage record** is
an observed quantity, not automatically an invoice line or access denial.

Catalogue defaults are fallback values, not proof that a feature is enforced.
The standard labels mark the capabilities without a complete enforcing consumer
(`video.jitsi.concurrent_rooms` and `support.priority`) as **Beta**, with
descriptions explaining the missing enforcement. Core enforces the video
module and API/webhook capabilities, so those carry no Beta label. Both
free-registration billing capabilities are also Beta:
their billing consumer works, but the allowance is per event within the invoice
period and does not incorporate add-on allowances. These labels are shared by
admin choices, plan details, and pricing; they do not change policy or billing.
`seed_standard_entitlements_for_version` creates missing rows without replacing
existing values. New organizers receive the published Free tier through
`signals.auto_assign_free_tier`; that tier can have administrator-edited values.
Read `tests/test_capabilities.py`, `test_seed_entitlements.py`, and
`test_entitlement_receiver.py` alongside this guide.

## Standard catalogue

The following describes the inspected core and Business `dev` source (core
includes fossasia/eventyay#6889). “No caller found” means no enforcing caller
was found in those repositories; another plugin could introduce one. Re-run the
searches below when branches or plugins change.

| Capability | Type / catalogue default | Meaning and actual consumer |
|---|---|---|
| `video.youtube` | boolean / true | YouTube streaming access. Core `base/services/video_entitlements.py` maps `livestream.youtube` to this capability. Enforced on room config (`check_video_entitlements`/`require_video_entitlements`, API and websocket `config_patch`), stream schedule serializers, and stream data (`filter_stream_by_entitlement`, which strips stream data when denied). |
| `video.jitsi` | boolean / true | Jitsi room access. `call.jitsi` modules are checked by the same video entitlement helpers on room create/update. |
| `video.jitsi.concurrent_rooms` | integer / 1 room | Intended simultaneous room limit. Evaluator compares caller-supplied quantity; it does not count running rooms. No core caller found. |
| `video.loungemesh` | boolean / false | Spatial networking access. `call.loungemesh` and `channel.loungemesh` modules are checked on room config, and loungemesh token exchange/refresh in `api/views/loungemesh.py` returns 403 `entitlement_denied`. |
| `email.bulk.monthly` | integer / 1000 emails | Enforced in core `app/eventyay/plugins/sendmail/views.py` and `app/eventyay/orga/views/mails.py`. Adds organizer-wide usage for the current month to requested quantity; counts queued/requested mail, not confirmed SMTP deliveries. |
| `organizer.full_admins` | integer / 2 admins | Core `app/eventyay/base/services/teams.py:check_full_admin_limit` compares existing full-admin members plus pending invitations plus the proposed addition. This is not a limit on every team member. |
| `api.read` | boolean / true | REST reads (safe methods) made with `Device`, `TeamAPIToken`, `UserApiToken` or OAuth credentials are checked in core `api/auth/permission.py` (also `api/auth/api_auth.py` and the organizer list in `api/views/organizer.py`). Browser sessions are not gated. |
| `api.write` | boolean / false | REST writes (non-safe methods) with the same credential types. Because `Device` tokens are gated, a check-in device redeeming a check-in (a POST) needs this capability. |
| `api.webhooks` | boolean / false | Webhook delivery is checked in core `api/webhooks.py` before notifications are queued and sent. Do not confuse the name with core's Python `eventyay.api.webhooks` module. |
| `commerce.platform_fee_percent` | decimal / 0% | Read by fee policy/ledger logic, not a boolean permission gate. Country/currency overrides take precedence over tier values; no matching policy means zero. |
| `registration.free_allowance_per_event` | integer / 100 registrations | Core order placement emits usage for zero-price admission positions excluding gift cards. Business invoice generation groups eligible usage by event and subtracts the tier allowance. No checkout hard-block caller found. |
| `registration.free_overage_price` | money / 0 | Business invoices multiply excess free registrations by this tier price. Zero price creates no positive overage line. Generic entitlement checks do not enforce money values. |
| `support.priority` | boolean / false | Intended support benefit. No automated core support-routing caller found. |

## Exact generic decision path

Core `app/eventyay/base/entitlements.py:check_entitlement` rejects an empty
capability, sends the signal, and defaults to allowed when no receiver denies.
An explicit false response or a denying `EntitlementDecision` wins.
Business `signals.enforce_entitlements` then:

1. Allows a staff/superuser **only if the caller supplies `user`** in keyword
   arguments. Do not assume all callers supply it.
2. Returns no decision for unknown capabilities. Registration is needed before
   the generic Business evaluator recognizes a custom key.
3. Selects an active or past-due subscription that has started. Active
   subscriptions need an absent or unexpired `ends_at`. Past-due subscriptions
   remain eligible even after `ends_at`, but are discarded if their grace period
   has expired.
4. Reads the selected tier's typed value, falling back to the catalogue default
   if missing or blank. Blank does not mean unlimited when the default is finite.
5. Collects active, started, non-ended, non-canceled organizer add-ons and, when
   an event is supplied, that event's add-ons. The definition must be active.
   Past-due add-ons participate when the selected subscription is past due and
   within grace. Assignment capability/value overrides are resolved by the
   assignment model, with definition fallbacks.
6. For booleans, allows if the base value or any add-on grants true; otherwise
   denies with `tier_restriction`. A false add-on does not revoke a true tier grant.
7. For integers, adds `assignment.quantity * typed allowance` for each eligible
   add-on (falling back to definition quantity where applicable). Non-monthly
   checks compare the requested **total** quantity, which the caller must compute.
   Keys ending in `.monthly` additionally aggregate organizer/capability usage
   for the current year/month, across events. `total > limit` denies with
   `tier_limit_exceeded`; equality is allowed. It does not reserve usage itself.
8. Other types, including decimal/money, return allowed; pricing consumers must
   interpret them separately.

`TierEntitlement.get_typed_value` maps true/1/yes to booleans, integer strings to
integers, and decimal/money strings to `Decimal`. Missing values return `None`.
Do not assume display formatting implements runtime policy. The generic evaluator
does not use `overage_allowed`, `overage_price`, or `overage_block_size` to bypass
a hard limit or automatically create charges.

## Consumer details and limitations

**Email:** ticket sendmail checks unique recipient count inside an organizer-row
transaction and records usage with a queue-derived idempotency key. Talk compose
checks recipients and records created queued rows; draft-to-outbox checks and
records one mail. These paths are not identical. Monthly aggregation is by
organizer and capability, not by event, even when a particular event add-on
contributes allowance. A denied limit does not automatically buy overage.

**Admin seats:** `check_full_admin_limit` ignores teams without
`can_change_organizer_settings`, skips users/emails already holding full-admin
access or already invited, locks the organizer row, counts distinct lowercased
member emails, and counts pending invitations excluding existing member emails.
Inspect callers and their transaction before extending the rule. The helper's
`user` argument identifies the proposed member, and is not passed as the generic
receiver's staff-bypass argument.

**Free registration:** core `app/eventyay/base/services/orders.py` emits usage at
order placement, not only payment confirmation. Business
`invoicing_service.generate_business_invoice_for_organizer` reads unclaimed usage
in the invoice interval, groups by event, and reads allowance/price directly from
the selected tier. Its fallback allowance/price starts at zero; it does not call
the generic add-on-aware evaluator. Consequently the catalogue's “per event”
label is not proof of lifetime-per-event accounting: the current calculation
uses the selected billing interval's records. Do not silently claim add-ons,
generic overage flags, or cancellation reversal alter that formula.

**Fees:** `services.resolve_fee_settings`, `signals.provide_platform_fee_policy`,
and `platform_fee_ledger.py` resolve and snapshot the policy at payment time.
They use country/currency overrides or the applicable tier, not the generic
boolean/integer decision. Decimal arithmetic, caps, cutover, and historical
already-collected amounts determine debt. See [billing flows](flows.md).

**Usage:** `services.record_usage` writes immediately and treats only the named
organizer/idempotency unique-constraint conflict as duplicate delivery. Other
integrity errors propagate. Usage collection, feature enforcement, invoice
calculation, and payment collection are separate operations.

## Examples and change checklist

- Monthly email limit 1000, usage 950, proposed 50: allowed. Proposed 51: denied.
  An eligible add-on contributing 500 raises the effective limit to 1500.
- Two existing full-admin seats under limit 2: a new admin is denied; an existing
  admin joining another admin team is not an extra seat under the helper.
- A boolean false tier plus a true eligible add-on permits the feature **when a
  caller actually asks**. `api.write=false` does block token, device and OAuth
  writes in core, but not browser-session writes.
- Invoice-period free registrations 120, tier allowance 100, unit overage 0.20:
  the dedicated registration calculation produces 4.00 before other invoice
  adjustments. This is not a checkout rejection.

For a new capability, update registry/type/default, tier seeding/admin selection,
caller enforcement, usage identity (if metered), pricing/invoice behavior (if
billable), and tests separately. Add-ons alone do not create enforcement callers.
Relevant tests: `tests/test_addon_entitlements.py`, `test_event_addons.py`,
`test_invoices.py`, `test_fee_policy_provider.py`, plus core
`app/tests/base/test_entitlements.py` and affected mail/team tests.

To audit wiring from a core checkout:

```sh
rg -n 'check_entitlement|entitlement_usage_recorded|record_usage' app/eventyay
rg -n 'capability|overage' plugins/eventyay-business/eventyay_business/{signals,services,invoicing_service,models}.py
```

Repeat literal-key searches for each new capability in the relevant external
plugins. Record missing enforcement as a gap; do not invent implementation from
the catalogue description or an issue's target behavior.
