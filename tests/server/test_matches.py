from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch, AsyncMock
from datetime import timedelta

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.authtoken.models import Token

from config.asgi import application
from core import matches
from core.models import Profile, Match, QueueEntry


class MatchTests(TransactionTestCase):
    def setUp(self):
        self.users = [get_user_model().objects.create_user(username=f"match_{i}") for i in range(4)]
        for user in self.users:
            Profile.objects.create(user=user)

    def pair(self):
        for user in self.users[:2]:
            matches.action(user.pk, {"type": "queue.join"})
        return Match.objects.get()

    def move(self, match, cell, user=None, **extra):
        match.refresh_from_db()
        return matches.action(user or (match.x_id if match.turn == "X" else match.o_id),
                              {"type": "match.move", "match_id": match.pk,
                               "version": match.version, "cell": cell, **extra})

    def test_queue_is_idempotent_and_cancel_does_not_resign(self):
        uid = self.users[0].pk
        matches.action(uid, {"type": "queue.join"})
        matches.action(uid, {"type": "queue.join"})
        self.assertEqual(QueueEntry.objects.count(), 1)
        self.assertEqual(Match.objects.count(), 0)
        matches.action(uid, {"type": "queue.cancel"})
        self.assertFalse(matches.state(uid)["searching"])
        match = self.pair()
        matches.action(uid, {"type": "queue.cancel"})
        match.refresh_from_db()
        self.assertEqual(match.result, "")
        self.assertIsNotNone(matches.state(uid)["match"])

    def test_all_moves_win_and_ratings_updated(self):
        match = self.pair()
        for cell in [0, 3, 1, 4, 2]:
            self.move(match, cell)
        match.refresh_from_db()
        self.assertEqual(match.result, "X")
        self.assertEqual(match.reason, "line")
        self.assertIsNotNone(match.finished_at)
        with self.assertRaises(matches.MatchError):
            self.move(match, 8)
        self.assertEqual(Profile.objects.get(user_id=match.x_id).rating, 1016)
        self.assertEqual(Profile.objects.get(user_id=match.o_id).rating, 984)

    def test_draw(self):
        match = self.pair()
        for cell in [0, 1, 2, 4, 3, 5, 7, 6, 8]:
            self.move(match, cell)
        match.refresh_from_db()
        self.assertEqual(match.result, "draw")

    def test_wrong_player_turn_cell_and_replayed_version(self):
        match = self.pair()
        for uid, cell in [(self.users[2].pk, 0), (match.o_id, 0), (match.x_id, -1), (match.x_id, True)]:
            with self.assertRaises(matches.MatchError):
                self.move(match, cell, uid)
        self.move(match, 0)
        with self.assertRaises(matches.MatchError):
            self.move(match, 1, version=0)
        with self.assertRaises(matches.MatchError):
            self.move(match, 0)
        self.assertEqual(matches.state(self.users[2].pk)["match"], None)
        match.refresh_from_db()
        self.assertEqual(match.board.count("X"), 1)
        self.assertEqual(match.board.count("O"), 0)

    def test_resign_dismiss_and_requeue(self):
        match = self.pair()
        with self.assertRaises(matches.MatchError):
            matches.action(match.x_id, {"type": "match.dismiss", "match_id": match.pk})
        matches.action(match.x_id, {"type": "match.resign", "match_id": match.pk})
        self.assertEqual(matches.state(match.o_id)["match"]["result"], "O")
        matches.action(match.x_id, {"type": "match.dismiss", "match_id": match.pk})
        matches.action(match.x_id, {"type": "queue.join"})
        self.assertTrue(matches.state(match.x_id)["searching"])

    def test_disconnect_timeout_and_reconnect_before_deadline(self):
        match = self.pair()
        Match.objects.filter(pk=match.pk).update(x_seen=timezone.now() - timedelta(seconds=30))
        self.assertEqual(matches.state(match.x_id, touch=True)["match"]["result"], "")
        Match.objects.filter(pk=match.pk).update(x_seen=timezone.now() - timedelta(seconds=46))
        self.assertEqual(matches.state(match.o_id, touch=True)["match"]["result"], "O")
        self.assertEqual(matches.state(match.x_id, touch=True)["match"]["result"], "O")

    def test_both_disconnected_and_expired_queue(self):
        matches.action(self.users[2].pk, {"type": "queue.join"})
        QueueEntry.objects.update(seen_at=timezone.now() - timedelta(seconds=21))
        self.assertFalse(matches.state(self.users[2].pk, touch=True)["searching"])
        match = self.pair()
        Match.objects.filter(pk=match.pk).update(x_seen=timezone.now() - timedelta(seconds=46),
                                               o_seen=timezone.now() - timedelta(seconds=46))
        self.assertEqual(matches.state(match.x_id, touch=True)["match"]["result"], "abandoned")

    def test_parallel_search_cannot_create_duplicate_matches(self):
        def join(uid):
            close_old_connections()
            try:
                matches.action(uid, {"type": "queue.join"})
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(join, [user.pk for user in self.users] * 2))
        self.assertEqual(Match.objects.count(), 2)
        players = [uid for match in Match.objects.all() for uid in (match.x_id, match.o_id)]
        self.assertEqual(len(set(players)), 4)
        self.assertEqual(QueueEntry.objects.count(), 0)

    def test_two_websockets_receive_moves_and_reconnect(self):
        keys = [Token.objects.create(user=user).key for user in self.users[:2]]
        def socket(index):
            return WebsocketCommunicator(application, "/ws/session/", headers=[
                (b"authorization", f"Token {keys[index]}".encode())])
        async def wait_state(ws, predicate):
            for _ in range(10):
                event = await ws.receive_json_from(timeout=3)
                if event["type"] == "session.state" and predicate(event):
                    return event
            self.fail("Expected state not received")
        async def check():
            a, b = socket(0), socket(1)
            for ws in (a, b):
                self.assertTrue((await ws.connect())[0])
                await ws.receive_json_from()
                await ws.send_json_to({"type": "queue.join"})
                if ws is a:
                    await wait_state(a, lambda e: e["searching"])
            state_a = await wait_state(a, lambda e: e["match"] is not None)
            state_b = await wait_state(b, lambda e: e["match"] is not None)
            self.assertEqual(state_a["match"]["id"], state_b["match"]["id"])
            await a.send_json_to({"type": "match.move", "match_id": state_a["match"]["id"], "version": 0, "cell": 0})
            await wait_state(b, lambda e: e["match"] and e["match"]["board"][0] == "X")
            await a.disconnect()
            a = socket(0)
            await a.connect()
            restored = await a.receive_json_from()
            self.assertEqual(restored["match"]["board"][0], "X")
            self.assertEqual(restored["match"]["turn"], "O")
            await a.disconnect()
            await b.disconnect()
        async_to_sync(check)()

    def test_committed_move_survives_notification_failure(self):
        match = self.pair()
        key = Token.objects.create(user_id=match.x_id).key
        async def check():
            socket = WebsocketCommunicator(application, "/ws/session/", headers=[(b"authorization", f"Token {key}".encode())])
            await socket.connect()
            await socket.receive_json_from()
            with patch("core.social.get_channel_layer") as layer:
                layer.return_value.group_send = AsyncMock(side_effect=ConnectionError("Redis unavailable"))
                with self.assertLogs("core.social", level="ERROR"):
                    await socket.send_json_to({"type": "match.move", "match_id": match.pk, "version": 0, "cell": 0})
                    response = await socket.receive_json_from(timeout=3)
            self.assertEqual(response["match"]["board"][0], "X")
            await socket.send_json_to({"type": "session.sync"})
            self.assertEqual((await socket.receive_json_from())["match"]["version"], 1)
            await socket.disconnect()
        async_to_sync(check)()
