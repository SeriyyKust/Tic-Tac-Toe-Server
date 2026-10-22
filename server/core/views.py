import asyncio
import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import connection
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

logger = logging.getLogger(__name__)


async def check_channel_layer():
    layer = get_channel_layer()
    async with asyncio.timeout(2):
        channel = await layer.new_channel()
        await layer.send(channel, {"type": "health.check"})
        event = await layer.receive(channel)
        if event != {"type": "health.check"}:
            raise RuntimeError("Unexpected channel response")


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def health(request):
    checks = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        checks["database"] = "ok"
    except Exception:
        logger.exception("Database health check failed")
        checks["database"] = "unavailable"
    try:
        async_to_sync(check_channel_layer)()
        checks["channel_layer"] = "ok"
    except Exception:
        logger.exception("Channel layer health check failed")
        checks["channel_layer"] = "unavailable"
    healthy = all(value == "ok" for value in checks.values())
    return Response({"status": "ok" if healthy else "unavailable", **checks},
                    status=200 if healthy else 503)
