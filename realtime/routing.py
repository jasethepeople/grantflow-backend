from django.urls import re_path

from . import consumers

websocket_urlpatterns = [
    re_path(
        r"^ws/sections/(?P<section_id>[0-9a-fA-F-]{36})/collab/$",
        consumers.DocumentCollabConsumer.as_asgi(),
    ),
]
