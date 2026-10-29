from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.test import TransactionTestCase
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import Membership, Organization, User
from grants.models import Grant, GrantSection
from grantflow_backend.asgi import application
from realtime import steps_store


class CollabConsumerTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="collab@example.org", password="password-c-123")
        self.org = Organization.objects.create(name="Collab Org")
        Membership.objects.create(user=self.user, organization=self.org, role=Membership.ROLE_EDITOR)
        self.grant = Grant.objects.create(organization=self.org, title="Collab Grant")
        self.section = GrantSection.objects.create(
            grant=self.grant, section_type="methodology", title="Methodology", order=1,
        )
        self.token = str(RefreshToken.for_user(self.user).access_token)
        self.url = f"/ws/sections/{self.section.id}/collab/?token={self.token}"

    def tearDown(self):
        import asyncio
        asyncio.run(steps_store.reset(self.section.id))

    @database_sync_to_async
    def _create_viewer(self):
        viewer = User.objects.create_user(email="viewer-collab@example.org", password="password-v-123")
        Membership.objects.create(user=viewer, organization=self.org, role=Membership.ROLE_VIEWER)
        return viewer

    async def test_connect_without_token_is_rejected(self):
        communicator = WebsocketCommunicator(application, f"/ws/sections/{self.section.id}/collab/")
        connected, _ = await communicator.connect()
        self.assertFalse(connected)
        await communicator.disconnect()

    async def test_connect_with_invalid_token_is_rejected(self):
        communicator = WebsocketCommunicator(application, f"/ws/sections/{self.section.id}/collab/?token=garbage")
        connected, _ = await communicator.connect()
        self.assertFalse(connected)
        await communicator.disconnect()

    async def test_nonmember_is_rejected(self):
        outsider = await database_sync_to_async(User.objects.create_user)(
            email="outsider@example.org", password="password-o-123"
        )
        token = str(RefreshToken.for_user(outsider).access_token)
        communicator = WebsocketCommunicator(application, f"/ws/sections/{self.section.id}/collab/?token={token}")
        connected, close_code = await communicator.connect()
        self.assertFalse(connected)
        await communicator.disconnect()

    async def test_connect_with_valid_token_sends_init(self):
        communicator = WebsocketCommunicator(application, self.url)
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        response = await communicator.receive_json_from()
        self.assertEqual(response["type"], "init")
        self.assertEqual(response["version"], 0)
        await communicator.disconnect()

    async def test_submit_step_broadcasts_to_other_connected_client(self):
        comm1 = WebsocketCommunicator(application, self.url)
        comm2 = WebsocketCommunicator(application, self.url)
        await comm1.connect()
        await comm1.receive_json_from()  # init
        await comm2.connect()
        await comm2.receive_json_from()  # init

        await comm1.send_json_to({"type": "submit", "version": 0, "steps": [{"stepType": "replace"}], "client_id": "c1"})

        msg_to_submitter = await comm1.receive_json_from()
        msg_to_other = await comm2.receive_json_from()

        self.assertEqual(msg_to_submitter["type"], "steps")
        self.assertEqual(msg_to_submitter["version"], 1)
        self.assertEqual(msg_to_other["version"], 1)
        self.assertEqual(msg_to_other["client_ids"], ["c1"])
        self.assertEqual(msg_to_other["steps"], [{"stepType": "replace"}])

        await comm1.disconnect()
        await comm2.disconnect()

    async def test_conflicting_submit_returns_steps_to_rebase_against(self):
        comm = WebsocketCommunicator(application, self.url)
        await comm.connect()
        await comm.receive_json_from()  # init

        await comm.send_json_to({"type": "submit", "version": 0, "steps": [{"a": 1}], "client_id": "c1"})
        await comm.receive_json_from()  # accepted, now at version 1

        # Stale resubmission still claiming version 0.
        await comm.send_json_to({"type": "submit", "version": 0, "steps": [{"a": 2}], "client_id": "c1"})
        response = await comm.receive_json_from()

        self.assertEqual(response["type"], "steps")
        self.assertEqual(response["version"], 1)
        self.assertEqual(response["steps"], [{"a": 1}])  # what they're missing, to rebase against

        await comm.disconnect()

    async def test_viewer_can_connect_but_cannot_submit(self):
        viewer = await self._create_viewer()
        token = str(RefreshToken.for_user(viewer).access_token)
        communicator = WebsocketCommunicator(application, f"/ws/sections/{self.section.id}/collab/?token={token}")
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        await communicator.receive_json_from()  # init

        await communicator.send_json_to({"type": "submit", "version": 0, "steps": [{"a": 1}], "client_id": "v1"})
        response = await communicator.receive_json_from()
        self.assertEqual(response["type"], "error")

        await communicator.disconnect()

    async def test_catch_up_returns_steps_since_given_version(self):
        comm = WebsocketCommunicator(application, self.url)
        await comm.connect()
        await comm.receive_json_from()  # init
        await comm.send_json_to({"type": "submit", "version": 0, "steps": [{"a": 1}, {"a": 2}], "client_id": "c1"})
        await comm.receive_json_from()

        await comm.send_json_to({"type": "catch_up", "version": 1})
        response = await comm.receive_json_from()
        self.assertEqual(response["type"], "steps")
        self.assertEqual(response["version"], 2)
        self.assertEqual(response["steps"], [{"a": 2}])

        await comm.disconnect()


class StepsStoreTests(TransactionTestCase):
    """Direct tests of the Redis-backed store, independent of the WebSocket layer."""

    def setUp(self):
        self.section_id = "11111111-1111-1111-1111-111111111111"

    def tearDown(self):
        import asyncio
        asyncio.run(steps_store.reset(self.section_id))

    async def test_version_starts_at_zero(self):
        version = await steps_store.get_version(self.section_id)
        self.assertEqual(version, 0)

    async def test_submit_advances_version(self):
        success, version = await steps_store.submit_steps(
            self.section_id, 0, [{"step": {"a": 1}, "client_id": "c1"}]
        )
        self.assertTrue(success)
        self.assertEqual(version, 1)

    async def test_submit_with_wrong_version_fails(self):
        await steps_store.submit_steps(self.section_id, 0, [{"step": {"a": 1}, "client_id": "c1"}])
        success, version = await steps_store.submit_steps(
            self.section_id, 0, [{"step": {"a": 2}, "client_id": "c1"}]
        )
        self.assertFalse(success)
        self.assertEqual(version, 1)  # actual current version, for the caller to catch up against

    async def test_reset_clears_steps(self):
        await steps_store.submit_steps(self.section_id, 0, [{"step": {"a": 1}, "client_id": "c1"}])
        await steps_store.reset(self.section_id)
        version = await steps_store.get_version(self.section_id)
        self.assertEqual(version, 0)

    def test_reset_sync_clears_steps(self):
        import asyncio
        asyncio.run(steps_store.submit_steps(self.section_id, 0, [{"step": {"a": 1}, "client_id": "c1"}]))
        steps_store.reset_sync(self.section_id)
        version = asyncio.run(steps_store.get_version(self.section_id))
        self.assertEqual(version, 0)
