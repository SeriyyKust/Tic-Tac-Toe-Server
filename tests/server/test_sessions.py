from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase
from asgiref.sync import async_to_sync
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from config.asgi import application
from core.models import Profile


class SessionTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="socket_player", password="test-pass-178!")
        Profile.objects.create(user=self.user)
        self.key = Token.objects.create(user=self.user).key

    def socket(self, key=None):
        return WebsocketCommunicator(application, "/ws/session/", headers=[
            (b"authorization", f"Token {key or self.key}".encode())])

    def test_reject_missing_invalid_and_inactive_tokens(self):
        async def check():
            for socket in [WebsocketCommunicator(application, "/ws/session/"), self.socket("invalid")]:
                self.assertFalse((await socket.connect())[0])
                await socket.disconnect()
            await database_sync_to_async(get_user_model().objects.filter(pk=self.user.pk).update)(is_active=False)
            socket = self.socket()
            self.assertFalse((await socket.connect())[0])
            await socket.disconnect()
        async_to_sync(check)()

    def test_sync_reconnect_and_invalid_commands(self):
        async def check():
            socket = self.socket()
            self.assertTrue((await socket.connect())[0])
            state = await socket.receive_json_from()
            self.assertEqual(state["profile"]["id"], self.user.pk)
            self.assertNotIn("token", state)
            for raw in ["null", "[]", "bad", '{"type":"move"}', '{"type":[]}', '{"type":{}}']:
                await socket.send_to(text_data=raw)
                self.assertEqual((await socket.receive_json_from())["code"], "invalid_message")
            await database_sync_to_async(Profile.objects.filter(user=self.user).update)(rating=1016)
            await socket.send_json_to({"type": "session.sync"})
            self.assertEqual((await socket.receive_json_from())["profile"]["rating"], 1016)
            await socket.disconnect()
            socket = self.socket()
            await socket.connect()
            self.assertEqual((await socket.receive_json_from())["profile"]["rating"], 1016)
            await socket.send_to(bytes_data=b"bad")
            self.assertEqual((await socket.receive_output())["code"], 1009)
            await socket.disconnect()
        async_to_sync(check)()

    def test_logout_closes_all_connections(self):
        async def check():
            sockets = [self.socket(), self.socket()]
            for socket in sockets:
                await socket.connect()
                await socket.receive_json_from()
            def logout():
                client = APIClient()
                client.credentials(HTTP_AUTHORIZATION=f"Token {self.key}")
                return client.post("/api/auth/logout/").status_code
            self.assertEqual(await database_sync_to_async(logout)(), 204)
            for socket in sockets:
                self.assertEqual((await socket.receive_output(timeout=3))["code"], 4401)
                await socket.disconnect()
            socket = self.socket()
            self.assertFalse((await socket.connect())[0])
            await socket.disconnect()
        async_to_sync(check)()

    def test_token_checked_even_without_redis_notification(self):
        async def check():
            socket = self.socket()
            await socket.connect()
            await socket.receive_json_from()
            await database_sync_to_async(Token.objects.filter(key=self.key).delete)()
            await socket.send_json_to({"type": "ping"})
            self.assertEqual((await socket.receive_output())["code"], 4401)
            await socket.disconnect()
        async_to_sync(check)()
