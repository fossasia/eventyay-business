"""Helpers for reading Stripe SDK response objects.

Stripe Python SDK 15.0 stopped making ``StripeObject`` a ``dict`` subclass, so
``.get()``, ``.keys()``, ``.items()`` and ``dict(obj)`` raise on SDK objects.
Code that reads Stripe payloads with dict semantics should convert them here
first. The helpers stay compatible with older SDKs, where ``StripeObject`` is
still a ``dict`` and ``to_dict()`` is deprecated and only converts one level.
"""


def stripe_to_dict(obj) -> dict:
    """Return a Stripe payload as something that supports dict access.

    Accepts plain dicts (tests, synthesized payloads), ``StripeObject`` instances
    from any supported SDK version, and ``None`` for absent nested fields.
    """
    if obj is None:
        return {}
    # Plain dicts, and StripeObject on SDKs older than 15, already behave like
    # dicts. Skipping to_dict() there avoids its deprecation warning.
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    raise TypeError(f"Expected a Stripe object or dict, got {type(obj).__name__}")
