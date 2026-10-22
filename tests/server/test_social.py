from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from config.asgi import application
from core import social, matches
from core.models import Profile, Friendship, Invitation, Presence, Match, QueueEntry


class SocialTests(TransactionTestCase):
    def setUp(self):
        self.users = [get_user_model().objects.create_user(username=f"social_{i}") for i in range(3)]
        self.keys = []
        for user in self.users:
            Profile.objects.create(user=user)
            self.keys.append(Token.objects.create(user=user).key)
        self.a, self.b, self.c = [user.pk for user in self.users]
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.keys[0]}")

    def befriend(self, a=None, b=None):
        a, b = a or self.a, b or self.b
        social.friendship_action(a, "friend.request", b)
        social.friendship_action(b, "friend.accept", a)
        social.touch(a, f"test.{a}")
        social.touch(b, f"test.{b}")

    def invite(self):
        social.invite_action(self.a, {"type": "invite.send", "user_id": self.b})
        return Invitation.objects.get(sender_id=self.a, recipient_id=self.b)

    def test_friend_lifecycle_and_direction_permissions(self):
        social.friendship_action(self.a, "friend.request", self.b)
        social.friendship_action(self.a, "friend.request", self.b)
        self.assertEqual(Friendship.objects.count(), 1)
        self.assertFalse(social.snapshot(self.a)["requests"][0]["incoming"])
        self.assertTrue(social.snapshot(self.b)["requests"][0]["incoming"])
        with self.assertRaises(matches.MatchError):
            social.friendship_action(self.a, "friend.accept", self.b)
        with self.assertRaises(matches.MatchError):
            social.friendship_action(self.b, "friend.request", self.a)
        social.friendship_action(self.b, "friend.accept", self.a)
        self.assertEqual(len(social.snapshot(self.a)["friends"]), 1)
        social.friendship_action(self.a, "friend.remove", self.b)
        self.assertEqual(social.snapshot(self.b)["friends"], [])
        for target in [self.a, -1, True, "2", None]:
            with self.assertRaises(matches.MatchError):
                social.friendship_action(self.a, "friend.request", target)

    def test_reject_and_cancel_requests(self):
        for actor, target in [(self.b, self.a), (self.a, self.b)]:
            social.friendship_action(self.a, "friend.request", self.b)
            social.friendship_action(actor, "friend.remove", target)
            self.assertFalse(Friendship.objects.exists())

    def test_search_and_private_api(self):
        data = self.client.get("/api/users/?q=social").data["results"]
        self.assertEqual(len(data), 2)
        self.assertNotIn(self.a, [row["user_id"] for row in data])
        self.assertEqual(self.client.get("/api/users/?q=so").status_code, 400)
        self.assertEqual(self.client.post("/api/social/", {"type": "friend.request", "user_id": self.b}, format="json").status_code, 200)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.keys[2]}")
        self.assertEqual(self.client.get("/api/social/").data["requests"], [])
        self.assertEqual(self.client.post("/api/social/", {"type": "friend.accept", "user_id": self.a}, format="json").status_code, 400)
        self.client.credentials()
        self.assertEqual(self.client.get("/api/social/").status_code, 401)
        self.assertEqual(self.client.get("/api/users/?q=social").status_code, 401)

    def test_presence_multiple_sessions_and_expiry(self):
        self.befriend()
        social.touch(self.b, "second")
        Presence.objects.filter(channel=f"test.{self.b}").delete()
        self.assertTrue(social.snapshot(self.a)["friends"][0]["online"])
        Presence.objects.filter(user_id=self.b).update(seen_at=timezone.now() - timedelta(seconds=16))
        self.assertFalse(social.snapshot(self.a)["friends"][0]["online"])

    def test_invite_accept_creates_one_rated_match(self):
        self.befriend()
        invitation = self.invite()
        self.invite()
        self.assertEqual(Invitation.objects.count(), 1)
        QueueEntry.objects.create(user_id=self.a, seen_at=timezone.now())
        for _ in range(2):
            social.invite_action(self.b, {"type": "invite.accept", "invite_id": invitation.pk})
        self.assertEqual(Match.objects.count(), 1)
        self.assertFalse(QueueEntry.objects.exists())
        match = Match.objects.get()
        self.assertTrue(match.rated)
        self.assertEqual({match.x_id, match.o_id}, {self.a, self.b})
        matches.action(self.a, {"type": "match.resign", "match_id": match.pk})
        self.assertEqual(Profile.objects.get(user_id=self.b).rating, 1016)
        self.assertEqual(social.snapshot(self.b)["invitations"], [])

    def test_invite_requires_friend_online_available_and_recipient(self):
        with self.assertRaises(matches.MatchError):
            self.invite()
        self.befriend()
        invitation = self.invite()
        for uid in [self.a, self.c]:
            with self.assertRaises(matches.MatchError):
                social.invite_action(uid, {"type": "invite.accept", "invite_id": invitation.pk})
        Presence.objects.filter(user_id=self.a).delete()
        with self.assertRaises(matches.MatchError):
            social.invite_action(self.b, {"type": "invite.accept", "invite_id": invitation.pk})
        social.touch(self.a, "again")
        matches.action(self.a, {"type": "queue.join"})
        matches.action(self.c, {"type": "queue.join"})
        with self.assertRaises(matches.MatchError):
            social.invite_action(self.b, {"type": "invite.accept", "invite_id": invitation.pk})
        self.assertEqual(Match.objects.count(), 1)

    def test_expire_decline_cancel_and_unfriend(self):
        self.befriend()
        for mode in ["expire", "decline", "cancel", "remove"]:
            invitation = self.invite()
            if mode == "expire":
                Invitation.objects.filter(pk=invitation.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
            elif mode == "remove":
                social.friendship_action(self.a, "friend.remove", self.b)
            else:
                social.invite_action(self.b if mode == "decline" else self.a,
                                    {"type": f"invite.{mode}", "invite_id": invitation.pk})
            with self.assertRaises(matches.MatchError):
                social.invite_action(self.b, {"type": "invite.accept", "invite_id": invitation.pk})
            self.assertEqual(social.snapshot(self.b)["invitations"], [])
            Invitation.objects.all().delete()

    def test_parallel_accept_competes_with_random_match(self):
        self.befriend()
        invitation = self.invite()
        matches.action(self.a, {"type": "queue.join"})
        def run(action):
            close_old_connections()
            try:
                if action == "accept":
                    social.invite_action(self.b, {"type": "invite.accept", "invite_id": invitation.pk})
                else:
                    matches.action(self.c, {"type": "queue.join"})
            except matches.MatchError:
                pass
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(run, ["accept", "random", "accept"]))
        self.assertEqual(Match.objects.count(), 1)
        self.assertEqual(Profile.objects.filter(current_match__isnull=False).count(), 2)

    def test_websocket_presence_and_invite_delivery(self):
        self.befriend()
        Presence.objects.all().delete()
        async def check():
            def socket(index):
                return WebsocketCommunicator(application, "/ws/session/", headers=[(b"authorization", f"Token {self.keys[index]}".encode())])
            a, b = socket(0), socket(1)
            for ws in [a, b]:
                await ws.connect()
                await ws.receive_json_from()
            await a.send_json_to({"type": "invite.send", "user_id": self.b})
            event = await b.receive_json_from(timeout=3)
            invite = event["social"]["invitations"][0]
            self.assertTrue(invite["incoming"])
            await b.send_json_to({"type": "invite.accept", "invite_id": invite["id"]})
            for _ in range(5):
                event = await a.receive_json_from(timeout=3)
                if event.get("match"):
                    break
            self.assertTrue(event["match"]["rated"])
            await a.disconnect()
            await b.disconnect()
        async_to_sync(check)()
        self.assertFalse(Presence.objects.exists())
