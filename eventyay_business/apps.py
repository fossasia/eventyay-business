from django.utils.translation import gettext_lazy

from . import __version__

try:
    from eventyay.base.plugins import PluginConfig
except ImportError as e:
    if getattr(e, "name", None) != "eventyay.base.plugins":
        raise
    raise RuntimeError("Please use eventyay 2.7 or above to run this plugin!") from e


class PluginApp(PluginConfig):
    default = True
    name = "eventyay_business"
    verbose_name = "Eventyay Business"

    class EventyayPluginMeta:
        name = gettext_lazy("Eventyay Business")
        author = "eventyay team"
        description = gettext_lazy(
            "Eventyay plugin for tiers, add-ons, and billing restrictions"
        )
        visible = True
        version = __version__
        category = "FEATURE"
        compatibility = "eventyay>=2.7.0"
        settings_links = []
        navigation_links = []

    def ready(self):
        from . import signals  # NOQA
