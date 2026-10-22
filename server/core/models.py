from django.conf import settings
from django.db import models


class Profile(models.Model):
    """Игровая статистика пользователя и ссылка на его текущий матч."""

    id = models.BigAutoField(
        auto_created=True,
        primary_key=True,
        serialize=False,
        verbose_name="Идентификатор",
        help_text="Уникальный идентификатор записи в базе данных.",
    )
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
        verbose_name='Пользователь',
        help_text='Аккаунт Django, которому принадлежит профиль.',
    )
    rating = models.PositiveIntegerField(
        default=1000,
        verbose_name='Рейтинг Elo',
        help_text='Текущий рейтинг игрока; начальное значение — 1000.',
    )
    wins = models.PositiveIntegerField(
        default=0,
        verbose_name='Победы',
        help_text='Количество побед в рейтинговых матчах.',
    )
    losses = models.PositiveIntegerField(
        default=0,
        verbose_name='Поражения',
        help_text='Количество поражений в рейтинговых матчах.',
    )
    draws = models.PositiveIntegerField(
        default=0,
        verbose_name='Ничьи',
        help_text='Количество ничьих в рейтинговых матчах.',
    )
    current_match = models.ForeignKey(
        "Match",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        verbose_name='Текущий матч',
        help_text='Активный матч или завершённый матч, результат которого игрок ещё не закрыл.',
    )

    class Meta:
        verbose_name = 'Профиль игрока'
        verbose_name_plural = 'Профили игроков'


class Match(models.Model):
    """Состояние сетевой партии, её участники, результат и изменения рейтинга."""

    id = models.BigAutoField(
        auto_created=True,
        primary_key=True,
        serialize=False,
        verbose_name="Идентификатор",
        help_text="Уникальный идентификатор записи в базе данных.",
    )
    x = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="matches_x",
        verbose_name='Игрок X',
        help_text='Пользователь, играющий крестиками и делающий первый ход.',
    )
    o = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="matches_o",
        verbose_name='Игрок O',
        help_text='Пользователь, играющий ноликами.',
    )
    board = models.JSONField(
        default=list,
        verbose_name='Игровое поле',
        help_text='Девять клеток по строкам: пустая строка, X или O.',
    )
    turn = models.CharField(
        max_length=1,
        default="X",
        verbose_name='Очередь хода',
        help_text='Символ игрока, которому принадлежит следующий ход: X или O.',
    )
    result = models.CharField(
        max_length=10,
        blank=True,
        verbose_name='Результат',
        help_text='Пусто для активной игры; X, O, draw или abandoned для завершённой.',
    )
    reason = models.CharField(
        max_length=20,
        blank=True,
        verbose_name='Причина завершения',
        help_text='line — победная линия; board_full — ничья; resign — сдача; disconnect — отсутствие связи.',
    )
    version = models.PositiveIntegerField(
        default=0,
        verbose_name='Версия состояния',
        help_text='Увеличивается при изменении матча; защищает от устаревших команд.',
    )
    x_seen = models.DateTimeField(
        verbose_name='Последняя активность X',
        help_text='Время последнего обновления присутствия игрока X в матче.',
    )
    o_seen = models.DateTimeField(
        verbose_name='Последняя активность O',
        help_text='Время последнего обновления присутствия игрока O в матче.',
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name='Время создания',
        help_text='Дата и время создания партии.',
    )
    finished_at = models.DateTimeField(
        null=True,
        verbose_name='Время завершения',
        help_text='Дата и время фиксации результата; отсутствует у активной игры.',
    )
    rated = models.BooleanField(
        default=False,
        verbose_name='Рейтинговый матч',
        help_text='Определяет, должен ли результат влиять на Elo и статистику.',
    )
    rating_applied = models.BooleanField(
        default=False,
        verbose_name='Рейтинг начислен',
        help_text='Показывает, что изменения рейтинга и статистики сохранены.',
    )
    x_rating_before = models.PositiveIntegerField(
        null=True,
        verbose_name='Рейтинг X до матча',
        help_text='Рейтинг X перед начислением результата; заполняется при расчёте Elo.',
    )
    x_rating_after = models.PositiveIntegerField(
        null=True,
        verbose_name='Рейтинг X после матча',
        help_text='Рейтинг X после начисления результата.',
    )
    o_rating_before = models.PositiveIntegerField(
        null=True,
        verbose_name='Рейтинг O до матча',
        help_text='Рейтинг O перед начислением результата; заполняется при расчёте Elo.',
    )
    o_rating_after = models.PositiveIntegerField(
        null=True,
        verbose_name='Рейтинг O после матча',
        help_text='Рейтинг O после начисления результата.',
    )

    class Meta:
        verbose_name = 'Матч'
        verbose_name_plural = 'Матчи'
        constraints = [models.CheckConstraint(condition=~models.Q(x=models.F("o")), name="different_players")]


class QueueEntry(models.Model):
    """Ожидание пользователем случайного соперника."""

    id = models.BigAutoField(
        auto_created=True,
        primary_key=True,
        serialize=False,
        verbose_name="Идентификатор",
        help_text="Уникальный идентификатор записи в базе данных.",
    )
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        verbose_name='Игрок',
        help_text='Пользователь в очереди; допускается одна запись на аккаунт.',
    )
    seen_at = models.DateTimeField(
        verbose_name='Последняя активность',
        help_text='Время обновления очереди; используется для исключения устаревших записей.',
    )

    class Meta:
        verbose_name = 'Запись очереди'
        verbose_name_plural = 'Очередь поиска'


class Friendship(models.Model):
    """Уникальная пара пользователей с заявкой или подтверждённой дружбой."""

    id = models.BigAutoField(
        auto_created=True,
        primary_key=True,
        serialize=False,
        verbose_name="Идентификатор",
        help_text="Уникальный идентификатор записи в базе данных.",
    )
    low = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="friendships_low",
        verbose_name='Пользователь с меньшим ID',
        help_text='Первый участник упорядоченной пары пользователей.',
    )
    high = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="friendships_high",
        verbose_name='Пользователь с большим ID',
        help_text='Второй участник упорядоченной пары пользователей.',
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="friend_requests",
        verbose_name='Инициатор заявки',
        help_text='Участник пары, отправивший заявку в друзья.',
    )
    accepted = models.BooleanField(
        default=False,
        verbose_name='Заявка принята',
        help_text='Истина означает дружбу; ложь — ожидающую ответа заявку.',
    )

    class Meta:
        verbose_name = 'Дружба или заявка'
        verbose_name_plural = 'Дружба и заявки'
        constraints = [
            models.UniqueConstraint(fields=["low", "high"], name="unique_friend_pair"),
            models.CheckConstraint(condition=models.Q(low__lt=models.F("high")), name="ordered_friend_pair"),
        ]


class Presence(models.Model):
    """Активность отдельного WebSocket-соединения пользователя."""

    id = models.BigAutoField(
        auto_created=True,
        primary_key=True,
        serialize=False,
        verbose_name="Идентификатор",
        help_text="Уникальный идентификатор записи в базе данных.",
    )
    channel = models.CharField(
        max_length=255,
        unique=True,
        verbose_name='Канал соединения',
        help_text='Уникальное имя канала Channels для одного подключения.',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        verbose_name='Пользователь',
        help_text='Владелец соединения; у аккаунта может быть несколько каналов.',
    )
    seen_at = models.DateTimeField(
        db_index=True,
        verbose_name='Последняя активность',
        help_text='Время последнего обновления канала для определения статуса онлайн.',
    )

    class Meta:
        verbose_name = 'Присутствие'
        verbose_name_plural = 'Присутствие пользователей'


class Invitation(models.Model):
    """Приглашение друга в сетевую партию и результат его обработки."""

    id = models.BigAutoField(
        auto_created=True,
        primary_key=True,
        serialize=False,
        verbose_name="Идентификатор",
        help_text="Уникальный идентификатор записи в базе данных.",
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sent_invitations",
        verbose_name='Отправитель',
        help_text='Пользователь, предложивший игру; при принятии играет за X.',
    )
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="received_invitations",
        verbose_name='Получатель',
        help_text='Друг, которому предложена игра; при принятии играет за O.',
    )
    expires_at = models.DateTimeField(
        verbose_name='Срок действия',
        help_text='Время, после которого ожидающее приглашение нельзя принять.',
    )
    status = models.CharField(
        max_length=12,
        default="pending",
        verbose_name='Статус',
        help_text='pending — ожидание; accepted — принято; declined — отклонено; cancelled — отменено.',
    )
    match = models.ForeignKey(
        Match,
        null=True,
        on_delete=models.SET_NULL,
        verbose_name='Созданный матч',
        help_text='Матч, созданный при принятии приглашения; до принятия отсутствует.',
    )

    class Meta:
        verbose_name = 'Приглашение в матч'
        verbose_name_plural = 'Приглашения в матч'
