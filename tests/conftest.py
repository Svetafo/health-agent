"""Конфигурация pytest: фейковые env-переменные для тестов без .env."""

import os

# Подставляем до импорта src.config.settings — иначе pydantic падает
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0:test_token_for_tests")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ.setdefault("HEALTH_USER_ID", "123456789")
