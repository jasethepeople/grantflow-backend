"""
WebSocket consumer for real-time collaborative editing of a single GrantSection.

Protocol (JSON messages over the socket):

  Client -> Server
    {"type": "submit", "version": N, "steps": [<prosemirror step JSON>, ...], "client_id": "..."}

  Server -> Client
    {"type": "init", "version": N}
        Sent immediately on connect: the client should fetch the section's current
        SectionVersion content over REST for the baseline doc, then request/receive
        steps to catch up to `version` on top of it.
    {"type": "steps", "version": N, "steps": [...], "client_ids": [...]}
        Either a successful broadcast of newly accepted steps, OR — sent only to the
        submitter — the catch-up steps they need after a rejected submission (same
        shape, so the client's rebase logic doesn't need to special-case it).
    {"type": "error", "detail": "..."}

Auth: the connecting client passes a JWT access token as a query string param
(`?token=...`), since browsers can't set custom headers on a WebSocket handshake.
Only members of the section's organization may connect; only editors/admins/owners
may submit steps (viewers can watch but not write, mirroring the REST permissions).
"""

import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import AccessToken

from accounts.models import Membership
from grants.models import GrantSection

from . import steps_store


class DocumentCollabConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.section_id = self.scope["url_route"]["kwargs"]["section_id"]
        self.group_name = f"section_{self.section_id}_collab"

        user = await self._authenticate()
        if user is None:
            await self.close(code=4001)  # unauthorized
            return
        self.user = user

        membership = await self._get_membership()
        if membership is None:
            await self.close(code=4003)  # forbidden — not a member of this org
            return
        self.can_edit = membership.can_edit

        section_exists = await self._section_exists()
        if not section_exists:
            await self.close(code=4004)  # not found
            return

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        version = await steps_store.get_version(self.section_id)
        await self.send(text_data=json.dumps({"type": "init", "version": version}))

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            await self._send_error("Invalid JSON.")
            return

        message_type = data.get("type")
        if message_type == "submit":
            await self._handle_submit(data)
        elif message_type == "catch_up":
            await self._handle_catch_up(data)
        else:
            await self._send_error(f"Unknown message type '{message_type}'.")

    async def _handle_submit(self, data):
        if not self.can_edit:
            await self._send_error("You have view-only access to this section.")
            return

        client_version = data.get("version")
        steps = data.get("steps") or []
        client_id = data.get("client_id", "")

        if not isinstance(client_version, int) or not steps:
            await self._send_error("A submit message needs an integer 'version' and non-empty 'steps'.")
            return

        entries = [{"step": step, "client_id": client_id} for step in steps]
        success, resulting_version = await steps_store.submit_steps(self.section_id, client_version, entries)

        if success:
            await self.channel_layer.group_send(
                self.group_name,
                {
                    "type": "broadcast_steps",
                    "version": resulting_version,
                    "steps": steps,
                    "client_ids": [client_id] * len(steps),
                },
            )
        else:
            # Conflict: send the submitter exactly the steps they're missing so they can rebase.
            missing = await steps_store.get_steps_since(self.section_id, client_version)
            await self.send(text_data=json.dumps({
                "type": "steps",
                "version": resulting_version,
                "steps": [e["step"] for e in missing],
                "client_ids": [e["client_id"] for e in missing],
            }))

    async def _handle_catch_up(self, data):
        from_version = data.get("version", 0)
        if not isinstance(from_version, int):
            await self._send_error("catch_up needs an integer 'version'.")
            return
        missing = await steps_store.get_steps_since(self.section_id, from_version)
        current_version = await steps_store.get_version(self.section_id)
        await self.send(text_data=json.dumps({
            "type": "steps",
            "version": current_version,
            "steps": [e["step"] for e in missing],
            "client_ids": [e["client_id"] for e in missing],
        }))

    # channel_layer group event handler (name matches the "type" key with dots->underscores)
    async def broadcast_steps(self, event):
        await self.send(text_data=json.dumps({
            "type": "steps",
            "version": event["version"],
            "steps": event["steps"],
            "client_ids": event["client_ids"],
        }))

    async def _send_error(self, detail):
        await self.send(text_data=json.dumps({"type": "error", "detail": detail}))

    async def _authenticate(self):
        query_string = self.scope.get("query_string", b"").decode()
        params = dict(pair.split("=", 1) for pair in query_string.split("&") if "=" in pair)
        token = params.get("token")
        if not token:
            return None
        try:
            access_token = AccessToken(token)
        except TokenError:
            return None
        return await self._get_user(access_token["user_id"])

    @database_sync_to_async
    def _get_user(self, user_id):
        from accounts.models import User
        return User.objects.filter(id=user_id, is_active=True).first()

    @database_sync_to_async
    def _get_membership(self):
        section = GrantSection.objects.filter(id=self.section_id).select_related("grant__organization").first()
        if section is None:
            return None
        return Membership.objects.filter(user=self.user, organization=section.grant.organization).first()

    @database_sync_to_async
    def _section_exists(self):
        return GrantSection.objects.filter(id=self.section_id).exists()
