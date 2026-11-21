import copy


class ObjectWithoutDictMethods:
    """Stand-in for a stripe-python 15 ``StripeObject``.

    Supports attribute access, ``in``, ``[]`` and ``to_dict()``, but not dict methods
    such as ``.get()``, ``.items()`` or ``dict(obj)``, so it fails like a stripe-python 15
    object on any stripe version.
    """

    def __init__(self, data):
        self._data = data

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return _wrap(self._data[name])
        except KeyError:
            raise AttributeError(name) from None

    def __getitem__(self, key):
        return _wrap(self._data[key])

    def __contains__(self, key):
        return key in self._data

    def to_dict(self):
        return copy.deepcopy(self._data)


def _wrap(value):
    if isinstance(value, dict):
        return ObjectWithoutDictMethods(value)
    if isinstance(value, list):
        return [_wrap(item) for item in value]
    return value
