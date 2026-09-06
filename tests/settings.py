try:
    from eventyay.config.settings import *
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
