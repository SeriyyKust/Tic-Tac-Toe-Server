import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from django.core.asgi import get_asgi_application

django_application = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter
from django.urls import path
from core.consumers import DiagnosticConsumer, SessionConsumer

application = ProtocolTypeRouter({
    "http": django_application,
    "websocket": URLRouter([
        path("ws/diagnostics/", DiagnosticConsumer.as_asgi()),
        path("ws/session/", SessionConsumer.as_asgi()),
    ]),
})
