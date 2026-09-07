try:
    from eventyay.config.settings import *  # noqa: F403, F401
except ImportError:
    pass

SECRET_KEY = "test-secret-key"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}
STATIC_URL = "/static/"
USE_TZ = True
TIME_ZONE = "UTC"
