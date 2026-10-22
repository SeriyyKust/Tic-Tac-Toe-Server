from django.db.models import Q
from rest_framework.decorators import api_view
from rest_framework.pagination import PageNumberPagination

from .models import Match
from .matches import rating_data


class HistoryPagination(PageNumberPagination):
    page_size = 5


@api_view(["GET"])
def history(request):
    matches = Match.objects.filter(Q(x=request.user) | Q(o=request.user), finished_at__isnull=False
                                   ).select_related("x", "o").order_by("-finished_at", "-pk")
    paginator = HistoryPagination()
    page = paginator.paginate_queryset(matches, request)
    results = []
    for match in page:
        symbol = "X" if match.x_id == request.user.pk else "O"
        outcome = ("draw" if match.result == "draw" else "cancelled" if match.result == "abandoned"
                   else "win" if match.result == symbol else "loss")
        results.append({"id": match.pk, "opponent": match.o.username if symbol == "X" else match.x.username,
                        "symbol": symbol, "outcome": outcome, "reason": match.reason,
                        "finished_at": match.finished_at, **rating_data(match, request.user.pk)})
    return paginator.get_paginated_response(results)
