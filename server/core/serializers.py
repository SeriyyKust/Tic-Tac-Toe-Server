"""Сериализаторы входных данных API."""
from rest_framework import serializers


class Credentials(serializers.Serializer):
    username = serializers.RegexField(r"^[a-zA-Z0-9_]{3,30}$", max_length=30,
                                      error_messages={"invalid": "Логин: 3–30 латинских букв, цифр или _."})
    password = serializers.CharField(write_only=True, trim_whitespace=False, max_length=128)

    def validate_username(self, value):
        return value.lower()

