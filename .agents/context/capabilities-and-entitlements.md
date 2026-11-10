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
module and API/webhook capabilities, so those carry no Beta label. The
free-registration billing capabilities are not Beta either: Business invoices
count the allowance over each event's lifetime and include add-on allowances
(`eventyay_business/free_registrations.py`). These labels are shared by
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
| `registration.free_allowance_per_event` | integer / 100 registrations | Core order placement emits usage for zero-price admission positions excluding gift cards. Business invoice generation groups usage by event and subtracts what is left of the lifetime allowance (tier value plus add-on allowances). Billing only; no checkout hard-block. |
| `registration.free_overage_price` | money / 0 | Business invoices multiply free registrations beyond the remaining allowance by this tier price. Zero price creates no positive overage line. Generic entitlement checks do not enforce money values. |
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

**Video (not counted):** `video.youtube`, `video.jitsi` and `video.loungemesh`
are yes/no switches. Nothing records usage, meters minutes or viewers, or limits
the number of rooms; `video.jitsi.concurrent_rooms` is the only counted video
capability and has no caller. Core's `check_video_entitlements(event,
module_config)` maps module types to capabilities (`livestream.youtube` →
`video.youtube`, `call.jitsi` → `video.jitsi`, `call.loungemesh` and
`channel.loungemesh` → `video.loungemesh`) and asks for each with quantity 1 and
no `user`, so the organizer's plan decides, not the attendee, moderator or a
staff bypass. Allowed means the tier value is true or an eligible organizer or
event add-on grants it. Callers, as inspected in core `dev`:

| Where | Effect when denied |
|---|---|
| Room create (`base/services/event.py:_create_room`, API `RoomPermissions` POST) | `ValidationError` `entitlement_denied` / permission denied |
| Room update (API `RoomPermissions` PATCH/PUT, websocket `config_patch` in `features/live/modules/room.py`) | Rejected; websocket error `config.entitlement_denied` |
| Websocket actions requiring a module (`features/live/decorators.py`) | `room.entitlement_denied` |
| Room config sent to clients (`base/services/event.py`) | The denied module is omitted |
| Attendee room visibility (`base/settings.py:is_room_visible_for_attendee`) | Room hidden |
| YouTube stream schedules (`api/serializers/stream_schedule.py`) and stream data (`filter_stream_by_entitlement`, broadcast marker in `base/services/room.py`) | Schedule rejected; stream data stripped; plan change re-broadcasts |
| LoungeMesh token exchange/refresh (`api/views/loungemesh.py`) | 403 `entitlement_denied` |

Checks run on each request, so a downgrade applies to existing rooms without
deleting their configuration. BigBlueButton and other module types are not
mapped and are never gated.

**Email (counted):** “bulk” means mail an organizer writes and sends to a group
from a Compose screen. System and template mail (acceptance/rejection, schedule
notifications, reminders from fixed templates, order and account mail) is
deliberately not counted. `email.bulk.monthly` is checked and recorded by four
organizer compose actions; the talk draft-proposal reminder
(`orga/views/mails.py:ComposeDraftReminders`) is a fifth compose action that
counts one per reminder once fossasia/eventyay#7225 is merged. Usage is recorded when mail is queued, not when it is
delivered, and saving a draft is never counted.

| Path (core) | Checked against the limit | Recorded as usage |
|---|---|---|
| Ticket mail to attendees, `plugins/sendmail/views.py:SenderView` | unique recipient emails | unique recipient emails, key `ticket_bulk_mail_<queue pk>` |
| Ticket mail to teams, `plugins/sendmail/views.py:ComposeTeamsMail` | unique recipient emails | unique recipient emails |
| Talk compose to sessions/speakers and teams, `orga/views/mails.py:ComposeMailBaseView` | `len(form.get_recipients())` | number of `QueuedMail` rows created, key `bulk_mail_compose_<first pk>` |
| Talk draft to outbox, `orga/views/mails.py:DraftToOutbox` | 1 | 1, key `bulk_mail_draft_to_outbox_<pk>` |

The ticket paths check and record inside an organizer-row transaction; the talk
paths check before the transaction. These paths are not identical. Scheduled
ticket mails are counted when queued; `send_queued_mail` and
`SendEmailQueueView` only deliver an existing queue entry and neither check nor
record. Monthly aggregation is by organizer and capability for the current
calendar month, not by event, even when an event add-on contributes allowance.
A denied limit does not automatically buy overage.

**Email not counted, by design:** these have no entitlement call in core `dev`
and should stay that way:

- Talk outbox mail created by the system: acceptance/rejection and other
  submission state mails (`base/models/submission.py`), new-schedule speaker
  notifications (`base/models/schedule.py`), CfP question reminders
  (`orga/views/cfp.py`), speaker invitations (`orga/views/speaker.py`) and
  access-code mails (`base/models/access_code.py`). Talk `OutboxSend` and
  `MailDetail` send them from the outbox without a check.
- Transactional ticket mail: order confirmation, payment and expiry reminders,
  ticket downloads, waiting list, order import, bank transfer.
- Account and team mail: team invitations, password resets, organizer and
  administrator messages.

Because the outbox mixes compose mail (counted when queued) with system mail,
do not add counting to outbox sending: it would double count compose mail and
start counting system mail. Do not describe the capability as covering “all
email”.

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
in the invoice interval and groups it by event; `free_registrations.py` resolves
each event's position. The allowance is the selected tier's value (fallback zero,
not the catalogue default) plus add-on allowances: organizer add-ons apply to
every event, event add-ons to their event, each `quantity * typed value`. An
add-on counts when its definition is active, it is not pending, it started by
the period end, and no `ends_at`/`cancel_at`/`canceled_at` precedes the period
start; a canceled or expired add-on with none of those dates grants nothing. The allowance covers the event's lifetime: all usage recorded for the
event before the period start (invoiced or not) is subtracted first, and only
`period usage - remaining allowance` is billed, so an earlier overage is never
billed twice and never retroactively. Usage without an event shares one
organizer bucket with organizer add-ons only. The same helper feeds the
current-charges estimate in `billing_overview.py`, whose “Included” column shows
the allowance remaining at period start. It does not call the generic evaluator;
generic overage flags and order cancellation do not alter the formula, and
checkout is never blocked.

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
- Free registrations for an event with tier allowance 100 and unit overage 0.20:
  120 in the first period bills 20 (4.00); 30 more in the next period bills all
  30 (6.00). With 80 in the first period nothing is billed, and 30 in the next
  bills 10. An eligible 50-registration add-on raises the allowance to 150.
  This is not a checkout rejection.

For a new capability, update registry/type/default, tier seeding/admin selection,
caller enforcement, usage identity (if metered), pricing/invoice behavior (if
billable), and tests separately. Add-ons alone do not create enforcement callers.
Relevant tests: `tests/test_addon_entitlements.py`, `test_event_addons.py`,
`test_invoices.py`, `test_free_registrations.py`, `test_fee_policy_provider.py`, plus core
`app/tests/base/test_entitlements.py` and affected mail/team tests.

To audit wiring from a core checkout:

```sh
rg -n 'check_entitlement|entitlement_usage_recorded|record_usage' app/eventyay
rg -n 'capability|overage' plugins/eventyay-business/eventyay_business/{signals,services,invoicing_service,models}.py
```

Repeat literal-key searches for each new capability in the relevant external
plugins. Record missing enforcement as a gap; do not invent implementation from
the catalogue description or an issue's target behavior.
