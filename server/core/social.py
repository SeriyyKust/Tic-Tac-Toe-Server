from datetime import timedelta
import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from .models import Friendship, Invitation, Presence, Profile, QueueEntry
from .matches import locked, MatchError, start_match

ONLINE_SECONDS = 15
INVITE_SECONDS = 60


def pair(a, b):
    return {"low_id": min(a, b), "high_id": max(a, b)}


def online_ids():
    return set(Presence.objects.filter(seen_at__gte=timezone.now() - timedelta(seconds=ONLINE_SECONDS),
                                      user__is_active=True).values_list("user_id", flat=True))


def touch(user_id, channel):
    now = timezone.now()
    Presence.objects.update_or_create(channel=channel, defaults={"user_id": user_id, "seen_at": now})
    Presence.objects.filter(seen_at__lt=now - timedelta(seconds=ONLINE_SECONDS)).delete()


def snapshot(user_id):
    online = online_ids()
    result = {"friends": [], "requests": [], "invitations": []}
    relations = Friendship.objects.filter(Q(low_id=user_id) | Q(high_id=user_id)).select_related(
        "low__profile", "high__profile").order_by("pk")
    for relation in relations:
        other = relation.high if relation.low_id == user_id else relation.low
        profile = getattr(other, "profile", None)
        row = {"user_id": other.pk, "username": other.username}
        if relation.accepted:
            row.update(online=other.pk in online, rating=profile.rating if profile else 1000,
                       busy=bool(profile and profile.current_match_id))
            result["friends"].append(row)
        else:
            row["incoming"] = relation.requested_by_id != user_id
            result["requests"].append(row)
    invites = Invitation.objects.filter(Q(sender_id=user_id) | Q(recipient_id=user_id),
        status="pending", expires_at__gt=timezone.now()).select_related("sender", "recipient").order_by("pk")
    for invite in invites:
        incoming = invite.recipient_id == user_id
        other = invite.sender if incoming else invite.recipient
        result["invitations"].append({"id": invite.pk, "username": other.username,
            "incoming": incoming, "expires_at": invite.expires_at})
    return result


def notify(users):
    for uid in users:
        try:
            async_to_sync(get_channel_layer().group_send)(f"session.{uid}", {"type": "session.changed"})
        except Exception:
            logging.getLogger(__name__).exception("Social notification failed; heartbeat will refresh state")


def friendship_action(uid, kind, target):
    if type(target) is not int or uid == target or not get_user_model().objects.filter(pk=target, is_active=True).exists():
        raise MatchError("Выберите другого существующего пользователя.")
    with locked():
        relation = Friendship.objects.filter(**pair(uid, target)).first()
        if kind == "friend.request":
            if relation is None:
                Friendship.objects.create(**pair(uid, target), requested_by_id=uid)
            elif not relation.accepted and relation.requested_by_id != uid:
                raise MatchError("У вас уже есть входящая заявка от этого игрока. Примите её.")
        elif kind == "friend.accept":
            if relation is None or relation.requested_by_id == uid:
                raise MatchError("Входящая заявка не найдена.")
            relation.accepted = True
            relation.save(update_fields=["accepted"])
        elif kind == "friend.remove":
            if relation:
                relation.delete()  # Reject, cancel or unfriend, depending on state.
                Invitation.objects.filter(Q(sender_id=uid, recipient_id=target) | Q(sender_id=target, recipient_id=uid),
                                          status="pending").update(status="cancelled")
        else:
            raise MatchError("Неизвестное действие.")
    return {uid, target}


def invite_action(uid, message):
    with locked():
        kind = message["type"]
        if kind == "invite.send":
            target = message.get("user_id")
            if type(target) is not int or target == uid or not Friendship.objects.filter(**pair(uid, target), accepted=True).exists():
                raise MatchError("Пригласить можно только друга.")
            participants = {uid, target}
            if not participants.issubset(online_ids()):
                raise MatchError("Оба игрока должны быть онлайн.")
            if Profile.objects.filter(user_id__in=participants, current_match__isnull=False).exists():
                raise MatchError("Один из игроков занят. Сначала закройте предыдущий матч.")
            now = timezone.now()
            existing = Invitation.objects.filter(Q(sender_id=uid, recipient_id=target) | Q(sender_id=target, recipient_id=uid),
                status="pending", expires_at__gt=now).first()
            if not existing:
                Invitation.objects.create(sender_id=uid, recipient_id=target, expires_at=now + timedelta(seconds=INVITE_SECONDS))
            return participants
        invite_id = message.get("invite_id")
        if type(invite_id) is not int:
            raise MatchError("Приглашение не найдено.")
        invite = Invitation.objects.filter(pk=invite_id).first()
        if not invite or uid not in {invite.sender_id, invite.recipient_id}:
            raise MatchError("Приглашение не найдено.")
        if kind in {"invite.accept", "invite.decline"} and uid != invite.recipient_id:
            raise MatchError("Ответить может только получатель.")
        if kind == "invite.cancel" and uid != invite.sender_id:
            raise MatchError("Отменить может только отправитель.")
        participants = {invite.sender_id, invite.recipient_id}
        if kind == "invite.accept" and invite.status == "accepted":
            return participants  # Safe duplicate; never creates a second match.
        if invite.status != "pending" or invite.expires_at <= timezone.now():
            raise MatchError("Приглашение истекло или уже обработано.")
        if kind == "invite.accept":
            if not Friendship.objects.filter(**pair(*sorted(participants)), accepted=True).exists():
                raise MatchError("Вы больше не друзья.")
            if not participants.issubset(online_ids()):
                raise MatchError("Друг сейчас не в сети.")
            if Profile.objects.filter(user_id__in=participants, current_match__isnull=False).exists():
                raise MatchError("Один из игроков уже в матче.")
            invite.match = start_match(invite.sender_id, invite.recipient_id)
            invite.status = "accepted"
        elif kind in {"invite.decline", "invite.cancel"}:
            invite.status = "declined" if kind == "invite.decline" else "cancelled"
        else:
            raise MatchError("Неизвестное действие.")
        invite.save()
        return participants


@api_view(["GET", "POST"])
def social_api(request):
    if request.method == "POST":
        if not isinstance(request.data, dict):
            raise ValidationError({"detail": "Ожидается объект с действием и ID пользователя."})
        try:
            affected = friendship_action(request.user.pk, request.data.get("type"), request.data.get("user_id"))
        except MatchError as error:
            raise ValidationError({"detail": str(error)})
        notify(affected)
    return Response(snapshot(request.user.pk))


@api_view(["GET"])
def search_users(request):
    query = request.query_params.get("q", "").strip().lower()
    if not 3 <= len(query) <= 30:
        raise ValidationError({"detail": "Введите от 3 до 30 символов логина."})
    users = get_user_model().objects.filter(username__startswith=query, is_active=True).exclude(pk=request.user.pk).order_by("username")[:20]
    return Response({"results": [{"user_id": user.pk, "username": user.username} for user in users]})
