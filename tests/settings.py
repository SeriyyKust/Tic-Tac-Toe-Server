"""Настройки только для отдельного тестового Docker-окружения."""
import os

from config.settings import *  # noqa: F403

SECRET_KEY = os.environ['DJANGO_SECRET_KEY']
DEBUG = False
ALLOWED_HOSTS = ['testserver', 'localhost']
DATABASES['default']['TEST'] = {'NAME': os.environ['POSTGRES_TEST_DB']}
# Изолированный кеш лимитов запросов; параметры аутентификации остаются реальными.
CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
                      'LOCATION': 'tic-tac-toe-tests'}}
