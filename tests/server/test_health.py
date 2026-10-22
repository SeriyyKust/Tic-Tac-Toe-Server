from unittest.mock import patch

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.test import TestCase, SimpleTestCase

from config.asgi import application


class HealthTests(TestCase):
    def test_database_and_real_redis(self):
        response = self.client.get("/api/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "status": "ok", "database": "ok", "channel_layer": "ok",
        })

    @patch("core.views.check_channel_layer", side_effect=ConnectionError)
    def test_dependency_failure_is_not_reported_as_healthy(self, check):
        with self.assertLogs("core.views", level="ERROR"):
            response = self.client.get("/api/health/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["channel_layer"], "unavailable")


class SocketTests(SimpleTestCase):
    def test_protocol(self):
        async_to_sync(self.exercise_protocol)()

    async def exercise_protocol(self):
        socket = WebsocketCommunicator(application, "/ws/diagnostics/")
        connected, _ = await socket.connect()
        self.assertTrue(connected)
        self.assertEqual(await socket.receive_json_from(), {"type": "ready", "protocol": 1})
        for invalid in ["broken json", "[]", "null", '{"type":"move"}']:
            await socket.send_to(text_data=invalid)
            self.assertEqual((await socket.receive_json_from())["code"], "invalid_message")
        await socket.send_json_to({"type": "ping"})
        self.assertEqual(await socket.receive_json_from(), {"type": "pong"})
        await socket.send_to(text_data="x" * 1025)
        self.assertEqual((await socket.receive_output())["code"], 1009)
        await socket.disconnect()
