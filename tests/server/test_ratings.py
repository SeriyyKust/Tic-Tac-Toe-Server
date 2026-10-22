from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.test import TransactionTestCase, SimpleTestCase
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from core import matches
from core.models import Match, Profile
from core.ratings import elo_delta


class EloTests(SimpleTestCase):
    def test_equal_players(self):
        self.assertEqual(elo_delta(1000, 1000, 1), 16)
        self.assertEqual(elo_delta(1000, 1000, 0), -16)
        self.assertEqual(elo_delta(1000, 1000, 0.5), 0)

    def test_underdog_and_draw(self):
        self.assertEqual(elo_delta(1000, 1400, 1), 29)
        self.assertEqual(elo_delta(1000, 1400, 0.5), 13)
        self.assertEqual(elo_delta(1400, 1000, 0.5), -13)
        self.assertEqual(elo_delta(0, 0, 0), 0)


class RatingTests(TransactionTestCase):
    def setUp(self):
        self.x = get_user_model().objects.create_user(username="elo_x")
        self.o = get_user_model().objects.create_user(username="elo_o")
        Profile.objects.create(user=self.x)
        Profile.objects.create(user=self.o)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=self.x).key}")

    def match(self, **kwargs):
        return Match.objects.create(x=self.x, o=self.o, board=[""] * 9,
                                    x_seen=timezone.now(), o_seen=timezone.now(), **kwargs)

    def test_finish_once_with_concurrent_retries(self):
        match = self.match(rated=True)
        def finish(_):
            close_old_connections()
            try:
                matches.finish(Match.objects.get(pk=match.pk), "X", "resign")
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(finish, range(3)))
        x, o = Profile.objects.get(user=self.x), Profile.objects.get(user=self.o)
        self.assertEqual((x.rating, x.wins, o.rating, o.losses), (1016, 1, 984, 1))
        match.refresh_from_db()
        self.assertTrue(match.rating_applied)
        self.assertEqual((match.x_rating_before, match.x_rating_after), (1000, 1016))

    def test_draw_updates_stats_and_unequal_ratings(self):
        Profile.objects.filter(user=self.o).update(rating=1400)
        match = self.match(rated=True)
        matches.finish(match, "draw", "board_full")
        x, o = Profile.objects.get(user=self.x), Profile.objects.get(user=self.o)
        self.assertEqual((x.rating, o.rating, x.draws, o.draws), (1013, 1387, 1, 1))

    def test_abandoned_and_old_matches_do_not_change_ratings(self):
        matches.finish(self.match(rated=True), "abandoned", "disconnect")
        matches.finish(self.match(rated=False), "X", "line")
        self.assertEqual(Profile.objects.get(user=self.x).rating, 1000)
        self.assertEqual(Profile.objects.get(user=self.x).wins, 0)
        self.assertFalse(Match.objects.filter(rating_applied=True).exists())

    def test_timeout_updates_rating_and_repeated_state_is_safe(self):
        match = self.match(rated=True)
        Profile.objects.filter(user__in=[self.x, self.o]).update(current_match=match)
        Match.objects.filter(pk=match.pk).update(x_seen=timezone.now() - timedelta(seconds=46))
        for _ in range(3):
            self.assertEqual(matches.state(self.o.pk, touch=True)["match"]["rating_delta"], 16)
        self.assertEqual(Profile.objects.get(user=self.o).wins, 1)
        self.assertEqual(Profile.objects.get(user=self.x).losses, 1)

    def test_failure_rolls_back_both_ratings_and_result(self):
        match = self.match(rated=True)
        original = Profile.save
        def fail_second(instance, *args, **kwargs):
            if instance.user_id == self.o.pk:
                raise RuntimeError("simulated write failure")
            return original(instance, *args, **kwargs)
        with patch.object(Profile, "save", fail_second):
            with self.assertRaises(RuntimeError):
                matches.finish(match, "X", "line")
        match.refresh_from_db()
        self.assertEqual(match.result, "")
        self.assertFalse(match.rating_applied)
        self.assertEqual(Profile.objects.get(user=self.x).rating, 1000)
        matches.finish(match, "X", "line")
        self.assertEqual(Profile.objects.get(user=self.x).rating, 1016)

    def test_history_private_paginated_and_snapshots_immutable(self):
        self.match(rated=True)  # Unfinished match must not appear.
        ids = []
        for _ in range(6):
            match = self.match(rated=True)
            matches.finish(match, "X", "line")
            ids.append(match.pk)
        other = get_user_model().objects.create_user(username="unrelated")
        Match.objects.create(x=self.o, o=other, board=[""] * 9, x_seen=timezone.now(),
                             o_seen=timezone.now(), result="draw", finished_at=timezone.now())
        response = self.client.get("/api/me/history/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 6)
        self.assertEqual([row["id"] for row in response.data["results"]], list(reversed(ids))[:5])
        self.assertIsNotNone(response.data["next"])
        page2 = self.client.get("/api/me/history/?page=2")
        old = page2.data["results"][0]
        self.assertEqual((old["rating_before"], old["rating_after"], old["outcome"]), (1000, 1016, "win"))
        self.assertEqual(self.client.get("/api/me/history/?page=999").status_code, 404)
        self.assertEqual(self.client.post("/api/me/history/", {}).status_code, 405)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=self.o).key}")
        loss = next(row for row in self.client.get("/api/me/history/?page=2").data["results"] if row["id"] == ids[0])
        self.assertEqual((loss["outcome"], loss["rating_delta"]), ("loss", -16))
        self.client.credentials()
        self.assertEqual(self.client.get("/api/me/history/").status_code, 401)

    def test_empty_history(self):
        self.assertEqual(self.client.get("/api/me/history/").data["results"], [])
