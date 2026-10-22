from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from rest_framework import serializers, status
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle

from .models import Profile
from .serializers import Credentials
import logging
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

User = get_user_model()


class AuthThrottle(AnonRateThrottle):
    rate = "30/min"


def profile_data(user):
    profile, _ = Profile.objects.get_or_create(user=user)
    return {"id": user.pk, "username": user.username, "joined_at": user.date_joined,
            "rating": profile.rating, "wins": profile.wins,
            "losses": profile.losses, "draws": profile.draws}


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([AuthThrottle])
def register(request):
    credentials = Credentials(data=request.data)
    credentials.is_valid(raise_exception=True)
    data = credentials.validated_data
    try:
        validate_password(data["password"], User(username=data["username"]))
    except DjangoValidationError as error:
        raise serializers.ValidationError({"password": error.messages})
    try:
        with transaction.atomic():
            user = User.objects.create_user(**data)
            Profile.objects.create(user=user)
            token = Token.objects.create(user=user)
    except IntegrityError:
        raise serializers.ValidationError({"username": ["Этот логин уже занят."]})
    return Response({"token": token.key, "profile": profile_data(user)}, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([AuthThrottle])
def login(request):
    credentials = Credentials(data=request.data)
    credentials.is_valid(raise_exception=True)
    user = authenticate(request=request, **credentials.validated_data)
    if user is None:
        return Response({"detail": "Неверный логин или пароль."}, status=400)
    token, _ = Token.objects.get_or_create(user=user)
    return Response({"token": token.key, "profile": profile_data(user)})


@api_view(["POST"])
def logout(request):
    request.auth.delete()
    from .models import Presence
    Presence.objects.filter(user=request.user).delete()
    from .matches import action
    action(request.user.pk, {"type": "queue.cancel"})
    try:
        async_to_sync(get_channel_layer().group_send)(
            f"session.{request.user.pk}", {"type": "session.revoked"})
    except Exception:
        # Token revocation remains effective even if Redis is unavailable.
        logging.getLogger(__name__).exception("Could not broadcast session revocation")
    return Response(status=204)


@api_view(["GET"])
def me(request):
    return Response(profile_data(request.user))
