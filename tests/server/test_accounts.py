from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework.test import APITestCase

from core.models import Profile


class AccountTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.credentials = {"username": "Player_One", "password": "A-strange-passphrase-491!"}

    def register(self):
        return self.client.post("/api/auth/register/", self.credentials, format="json")

    def test_registration_profile_login_logout(self):
        response = self.register()
        self.assertEqual(response.status_code, 201)
        user = get_user_model().objects.get()
        self.assertTrue(user.check_password(self.credentials["password"]))
        self.assertNotEqual(user.password, self.credentials["password"])
        self.assertEqual(response.data["profile"]["username"], "player_one")
        token = response.data["token"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        profile = self.client.get("/api/me/")
        self.assertEqual(profile.status_code, 200)
        self.assertEqual(profile.data["rating"], 1000)
        self.assertNotIn("password", profile.data)
        self.assertEqual(self.client.post("/api/auth/logout/").status_code, 204)
        self.assertEqual(self.client.get("/api/me/").status_code, 401)
        self.client.credentials()
        response = self.client.post("/api/auth/login/", self.credentials)
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(token, response.data["token"])

    def test_duplicate_case_insensitive_login_rolls_back(self):
        self.register()
        self.credentials["username"] = "PLAYER_ONE"
        self.assertEqual(self.register().status_code, 400)
        self.assertEqual(get_user_model().objects.count(), 1)
        self.assertEqual(Profile.objects.count(), 1)

    def test_weak_password_and_invalid_names_rejected(self):
        self.credentials["password"] = "12345678"
        self.assertEqual(self.register().status_code, 400)
        self.credentials["username"] = "bad name"
        self.assertEqual(self.register().status_code, 400)
        self.assertEqual(get_user_model().objects.count(), 0)

    def test_private_endpoints_require_token(self):
        self.assertEqual(self.client.get("/api/me/").status_code, 401)
        self.assertEqual(self.client.post("/api/auth/logout/").status_code, 401)

    def test_bad_password_and_inactive_user(self):
        self.register()
        response = self.client.post("/api/auth/login/", {**self.credentials, "password": "wrong"})
        self.assertEqual(response.status_code, 400)
        get_user_model().objects.update(is_active=False)
        self.assertEqual(self.client.post("/api/auth/login/", self.credentials).status_code, 400)

    def test_profile_cannot_be_written_by_client(self):
        response = self.register()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {response.data['token']}")
        self.assertEqual(self.client.post("/api/me/", {"rating": 9999}).status_code, 405)
        self.assertEqual(self.client.get("/api/me/").data["rating"], 1000)

    def test_login_throttled(self):
        for _ in range(30):
            self.client.post("/api/auth/login/", {})
        self.assertEqual(self.client.post("/api/auth/login/", {}).status_code, 429)
