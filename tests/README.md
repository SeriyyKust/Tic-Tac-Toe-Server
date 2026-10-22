# Серверные тесты

Все автоматические тесты Django находятся в `tests/server/`, вне исходников сервера.
Клиентские тесты остаются в `client/tests/` и выполняются на хосте.

Запуск из корня проекта:

```bash
docker compose -f tests/compose.yaml run --build --rm tests
```

Один модуль:

```bash
docker compose -f tests/compose.yaml run --rm tests python manage.py test tests.server.test_matches --settings=tests.settings --noinput
```

Остановка тестовых сервисов:

```bash
docker compose -f tests/compose.yaml down
```

`tests/compose.yaml` создаёт отдельный проект `tic-tac-toe-tests` и свою сеть.
PostgreSQL и Redis доступны только внутри этой сети, порты на хост не публикуются.
База PostgreSQL хранится в tmpfs и не использует том разработки.

`test.env` содержит только тестовые настройки и не секретные тестовые пароли;
Compose передаёт их контейнерам через `env_file`. Этот файл хранится в репозитории.
Корневой `.env` для этих значений не используется. Django загружает `tests.settings`,
который наследует прикладные настройки и задаёт тестовую БД, hosts и отдельный кеш.
Django создаёт и удаляет базу с именем из `POSTGRES_TEST_DB`.

Тесты используют настоящие PostgreSQL и Redis: это необходимо для проверки
advisory-блокировки, конкурентных запросов и Channels. Подмена на SQLite не нужна.
Тестовая папка подключается к контейнеру только для чтения и не копируется в образ сервера.
Существующий сценарий `scripts/verify_live.py` остаётся отдельной проверкой запущенного
сервера разработки: он не входит в этот изолированный набор.
