"""Разрешения сетевых действий по политике и настройкам пользователя."""

from __future__ import annotations

import os
from typing import Literal

from astra_voice.core.policy import Policy, PolicyStatus
from astra_voice.core.settings import Settings

NetworkKind = Literal["download", "check_app", "check_models"]


def _policy_offline(value: object) -> bool:
    """Разбирает offline; неизвестное значение запрещает сеть."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("1", "true", "yes", "on"):
            return True
        if normalized in ("0", "false", "no", "off"):
            return False
    return True


def _policy_profile_offline(value: object) -> bool:
    """Пустой профиль и personal разрешают сеть; остальные запрещают."""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in ("", "personal")
    return True


class NetworkGate:
    """Проверяет разрешение перед сетевым действием, не выполняя запросов."""

    def __init__(self, settings: Settings, policy: Policy) -> None:
        self._settings = settings
        self._policy = policy

    def allowed(self, kind: NetworkKind) -> tuple[bool, str]:
        """Возвращает разрешение и понятную пользователю причину отказа."""
        refusal = self.refusal(kind)
        if refusal == "admin":
            if self._policy.status == PolicyStatus.INVALID:
                return False, "Не удалось проверить правила администратора: работа без сети"
            return False, "Задано администратором: работа без сети"
        if refusal == "offline":
            return False, "Включена работа без сети"
        if refusal == "settings":
            return False, "Проверка обновлений выключена в настройках"
        return True, ""

    def refusal(self, kind: NetworkKind) -> str:
        """Возвращает закрытый код причины отказа или пустую строку."""
        if kind not in ("download", "check_app", "check_models"):
            raise ValueError("Неизвестный вид сетевого действия")

        if self._policy.status == PolicyStatus.INVALID:
            return "admin"
        if _policy_offline(self._policy.values.get("offline")) or _policy_profile_offline(
            self._policy.values.get("profile")
        ):
            return "admin"
        if os.environ.get("HF_HUB_OFFLINE", "").lower() in ("1", "true", "yes", "on"):
            return "offline"
        if (kind == "check_app" and self._settings.check_app_updates is not True) or (
            kind == "check_models" and self._settings.check_model_updates is not True
        ):
            return "settings"
        return ""
