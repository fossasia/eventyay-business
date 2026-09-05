from django.utils.translation import gettext_lazy

from . import __version__

try:
    from eventyay.base.plugins import PluginConfig
except ImportError:
    raise RuntimeError("Please use eventyay 2.7 or above to run this plugin!")


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
