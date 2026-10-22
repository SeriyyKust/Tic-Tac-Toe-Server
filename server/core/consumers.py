import json
import logging

from channels.generic.websocket import AsyncWebsocketConsumer
from channels.generic.websocket import WebsocketConsumer
from asgiref.sync import async_to_sync
from django.core.serializers.json import DjangoJSONEncoder
from rest_framework.authtoken.models import Token
from .accounts import profile_data
from . import matches
from . import social
from .models import Presence


class DiagnosticConsumer(AsyncWebsocketConsumer):
    """Public connection probe. No user information or game actions."""

    async def connect(self):
        await self.accept()
        await self.send(text_data=json.dumps({"type": "ready", "protocol": 1}))

    async def receive(self, text_data=None, bytes_data=None):
        if text_data is None or len(text_data) > 1024:
            await self.close(code=1009)
            return
        try:
            message = json.loads(text_data)
        except ValueError:
            message = None
        if isinstance(message, dict) and message.get("type") == "ping":
            response = {"type": "pong"}
        else:
            response = {"type": "error", "code": "invalid_message"}
        await self.send(text_data=json.dumps(response))


class SessionConsumer(WebsocketConsumer):
    """Authenticated session; revalidate credentials before every response."""

    def current_user(self):
        token = Token.objects.select_related("user").filter(key=self.token_key, user__is_active=True).first()
        return token.user if token else None

    def connect(self):
        self.group = None
        headers = dict(self.scope["headers"])
        authorization = headers.get(b"authorization", b"").decode("ascii", errors="ignore").split()
        self.token_key = authorization[1] if len(authorization) == 2 and authorization[0] == "Token" else ""
        user = self.current_user()
        if user is None:
            self.close()  # Rejected handshake: HTTP 403, no private data sent.
            return
        self.group = f"session.{user.pk}"
        async_to_sync(self.channel_layer.group_add)(self.group, self.channel_name)
        self.accept()
        self.send_state(touch=True)

    def send_json(self, data):
        self.send(text_data=json.dumps(data, cls=DjangoJSONEncoder))

    def send_state(self, touch=False):
        user = self.current_user()
        if user is None:
            self.close(code=4401)
            return
        if touch:
            social.touch(user.pk, self.channel_name)
        snapshot = matches.state(user.pk, touch=touch)
        self.send_json({"type": "session.state", "protocol": 1,
                        "profile": profile_data(user), "social": social.snapshot(user.pk), **snapshot})

    def receive(self, text_data=None, bytes_data=None):
        user = self.current_user()
        if user is None:
            self.close(code=4401)
            return
        if text_data is None or len(text_data.encode("utf-8")) > 4096:
            self.close(code=1009)
            return
        try:
            message = json.loads(text_data)
        except ValueError:
            message = None
        kind = message.get("type") if isinstance(message, dict) else None
        if not isinstance(kind, str):
            kind = None
        if kind == "session.sync":
            self.send_state(touch=True)
        elif kind == "ping":
            self.send_state(touch=True)
        elif kind in {"queue.join", "queue.cancel", "match.move", "match.resign", "match.dismiss"}:
            try:
                affected = matches.action(user.pk, message)
                social.notify(affected)
            except matches.MatchError as error:
                self.send_json({"type": "error", "code": "invalid_action", "detail": str(error)})
            self.send_state(touch=True)
        elif kind in {"invite.send", "invite.accept", "invite.decline", "invite.cancel"}:
            try:
                social.touch(user.pk, self.channel_name)
                social.notify(social.invite_action(user.pk, message))
            except matches.MatchError as error:
                self.send_json({"type": "error", "code": "invalid_action", "detail": str(error)})
            self.send_state(touch=True)
        else:
            self.send_json({"type": "error", "code": "invalid_message"})

    def session_revoked(self, event):
        # An old logout event must not close a newly issued token's connection.
        if self.current_user() is None:
            self.close(code=4401)

    def session_changed(self, event):
        self.send_state()

    def disconnect(self, code):
        Presence.objects.filter(channel=self.channel_name).delete()
        if self.group:
            try:
                async_to_sync(self.channel_layer.group_discard)(self.group, self.channel_name)
            except Exception:
                logging.getLogger(__name__).exception("Channel cleanup failed; membership expires automatically")
