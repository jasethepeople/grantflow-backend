"""
ASGI config for grantflow_backend project.

HTTP requests go to Django as normal. WebSocket connections under /ws/ are
routed to the Channels consumers in realtime/routing.py. Auth on the
WebSocket side is handled inside the consumer itself (JWT via query param),
not via Channels' AuthMiddlewareStack — see realtime/consumers.py.
"""

import os

import django
from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "grantflow_backend.settings")
django.setup()

from realtime.routing import websocket_urlpatterns  # noqa: E402  (must import after django.setup())

application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": URLRouter(websocket_urlpatterns),
})
