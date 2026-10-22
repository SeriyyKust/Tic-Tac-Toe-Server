"""PostgreSQL serializes match transitions; Redis only delivers notifications."""
from contextlib import contextmanager
from datetime import timedelta

from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from .models import Match, Profile, QueueEntry, Invitation
from .ratings import elo_delta

GRACE_SECONDS = 45
QUEUE_SECONDS = 20
LINES = ((0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6),
         (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6))


@contextmanager
def locked():
    # One transaction lock for this initial small server prevents duplicate pairing
    # and lock-order deadlocks across queue/match/profile transitions.
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(742019)")
        yield


def finish(match, result, reason):
    with locked():
        if Match.objects.get(pk=match.pk).result:
            match.refresh_from_db()
            return
        if match.rated and result in {"X", "O", "draw"}:
            x = Profile.objects.get(user_id=match.x_id)
            o = Profile.objects.get(user_id=match.o_id)
            delta = elo_delta(x.rating, o.rating, {"X": 1, "O": 0, "draw": 0.5}[result])
            match.x_rating_before, match.o_rating_before = x.rating, o.rating
            x.rating += delta
            o.rating -= delta
            match.x_rating_after, match.o_rating_after = x.rating, o.rating
            if result == "draw":
                x.draws += 1
                o.draws += 1
            elif result == "X":
                x.wins += 1
                o.losses += 1
            else:
                o.wins += 1
                x.losses += 1
            x.save(update_fields=["rating", "wins", "losses", "draws"])
            o.save(update_fields=["rating", "wins", "losses", "draws"])
            match.rating_applied = True
        match.result, match.reason = result, reason
        match.finished_at = timezone.now()
        match.version += 1
        match.save()


def expire(match, now):
    if match and not match.result:
        x_gone = (now - match.x_seen).total_seconds() > GRACE_SECONDS
        o_gone = (now - match.o_seen).total_seconds() > GRACE_SECONDS
        if x_gone or o_gone:
            finish(match, "abandoned" if x_gone and o_gone else ("O" if x_gone else "X"), "disconnect")


def serialize(match, user_id):
    if match is None:
        return None
    symbol = "X" if match.x_id == user_id else "O"
    line = next((line for line in LINES if match.board[line[0]] and
                 len({match.board[i] for i in line}) == 1), None)
    return {"id": match.pk, "symbol": symbol,
            "opponent": match.o.username if symbol == "X" else match.x.username,
            "board": match.board, "turn": match.turn, "result": match.result,
            "reason": match.reason, "version": match.version, "winning_line": line,
            "grace_seconds": GRACE_SECONDS, **rating_data(match, user_id)}


def rating_data(match, user_id):
    prefix = "x" if match.x_id == user_id else "o"
    before = getattr(match, f"{prefix}_rating_before")
    after = getattr(match, f"{prefix}_rating_after")
    return {"rated": match.rated, "rating_applied": match.rating_applied,
            "rating_before": before, "rating_after": after,
            "rating_delta": after - before if match.rating_applied else None}


def state(user_id, touch=False):
    with locked():
        now = timezone.now()
        profile, _ = Profile.objects.get_or_create(user_id=user_id)
        match = profile.current_match
        expire(match, now)
        if touch:
            QueueEntry.objects.filter(user_id=user_id, seen_at__gte=now - timedelta(seconds=QUEUE_SECONDS)).update(seen_at=now)
            if match and not match.result:
                field = "x_seen" if match.x_id == user_id else "o_seen"
                setattr(match, field, now)
                match.save(update_fields=[field])
        searching = QueueEntry.objects.filter(user_id=user_id, seen_at__gte=now - timedelta(seconds=QUEUE_SECONDS)).exists()
        return {"searching": searching, "match": serialize(match, user_id)}


class MatchError(Exception):
    pass


def start_match(x_id, o_id):
    """Caller holds locked(); caller verifies availability."""
    now = timezone.now()
    match = Match.objects.create(x_id=x_id, o_id=o_id, board=[""] * 9, x_seen=now, o_seen=now, rated=True)
    participants = [x_id, o_id]
    Profile.objects.filter(user_id__in=participants).update(current_match=match)
    QueueEntry.objects.filter(user_id__in=participants).delete()
    Invitation.objects.filter(Q(sender_id__in=participants) | Q(recipient_id__in=participants), status="pending").update(status="cancelled")
    return match


def action(user_id, message):
    """Return users to notify only after committing. No client-supplied identity."""
    with locked():
        now = timezone.now()
        profile, _ = Profile.objects.get_or_create(user_id=user_id)
        match = profile.current_match
        expire(match, now)
        kind = message["type"]
        affected = {user_id}
        if kind == "queue.join":
            if match is not None:
                return {match.x_id, match.o_id}
            QueueEntry.objects.filter(seen_at__lt=now - timedelta(seconds=QUEUE_SECONDS)).delete()
            QueueEntry.objects.update_or_create(user_id=user_id, defaults={"seen_at": now})
            opponent = QueueEntry.objects.exclude(user_id=user_id).filter(
                user__is_active=True, user__profile__current_match__isnull=True).order_by("?").first()
            if opponent:
                # The waiting player starts; every match still has exactly two distinct users.
                match = start_match(opponent.user_id, user_id)
                affected.add(opponent.user_id)
        elif kind == "queue.cancel":
            QueueEntry.objects.filter(user_id=user_id).delete()
        elif kind in {"match.move", "match.resign", "match.dismiss"}:
            if match is None or type(message.get("match_id")) is not int or message["match_id"] != match.pk:
                raise MatchError("Матч не найден.")
            affected.update([match.x_id, match.o_id])
            if kind == "match.dismiss":
                if not match.result:
                    raise MatchError("Сначала завершите матч или сдайтесь.")
                profile.current_match = None
                profile.save(update_fields=["current_match"])
            elif kind == "match.resign":
                if not match.result:
                    finish(match, "O" if match.x_id == user_id else "X", "resign")
            else:
                symbol = "X" if match.x_id == user_id else "O"
                cell = message.get("cell")
                if match.result:
                    raise MatchError("Матч уже завершён.")
                if type(message.get("version")) is not int or message["version"] != match.version:
                    raise MatchError("Состояние изменилось. Повторите ход после обновления поля.")
                if symbol != match.turn:
                    raise MatchError("Сейчас ход соперника.")
                if type(cell) is not int or not 0 <= cell < 9 or match.board[cell]:
                    raise MatchError("Выберите свободную клетку.")
                match.board[cell] = symbol
                match.version += 1
                if any(all(match.board[i] == symbol for i in line) for line in LINES):
                    finish(match, symbol, "line")
                elif all(match.board):
                    finish(match, "draw", "board_full")
                else:
                    match.turn = "O" if symbol == "X" else "X"
                    match.save()
        else:
            raise MatchError("Неизвестная команда.")
        return affected
