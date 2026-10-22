from django.urls import path
from core.views import health
from core import accounts
from core.history import history
from core.social import social_api, search_users

urlpatterns = [
    path("api/health/", health, name="health"),
    path("api/auth/register/", accounts.register),
    path("api/auth/login/", accounts.login),
    path("api/auth/logout/", accounts.logout),
    path("api/me/", accounts.me),
    path("api/me/history/", history),
    path("api/social/", social_api),
    path("api/users/", search_users),
]
