def stripe_object_to_dict(obj) -> dict:
    """Return a Stripe API object as a dict that supports dict methods such as ``.get()``.

    Before stripe-python 15, ``StripeObject`` subclassed ``dict`` and is returned as is.
    From 15 on it no longer does, and ``to_dict()`` converts it recursively.
    """
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    return obj.to_dict()
