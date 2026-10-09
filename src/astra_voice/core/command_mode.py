"""Command delivery and the single publication gate (Command1 §8.5).

Text exists only in memory. The presentation callback receives fixed outcome
codes, never the recognized text. The caller owns recognition and recording.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from astra_voice.platform.cowork import DeliveryResult, normalize_text


@dataclass(frozen=True)
class SessionSnapshot:
    known: bool = False
    locked: bool = True
    active: bool = True
    preparing_for_sleep: bool = False
    lock_requested: bool = False
    supported: bool = True
    blocked_epoch: int = 0
    sleep_epoch: int = 0

    @property
    def allowed(self) -> bool:
        return (
            self.known
            and not self.locked
            and not self.lock_requested
            and self.supported
            and self.active
            and not self.preparing_for_sleep
        )


class CommandClient(Protocol):
    def submit(self, text: str, callback: Callable[[DeliveryResult], None]) -> None: ...

    def cancel(self) -> None: ...


@dataclass(frozen=True)
class CommandFeedback:
    result: DeliveryResult
    copied: bool | None

    @property
    def text(self) -> str:
        if self.result.outcome == "delivered":
            return "Передано помощнику"
        if self.result.reason == "model_untrusted":
            return "Команда помощнику недоступна для модели, заданной вручную"
        prefix = (
            "Не удалось узнать, принята ли команда"
            if self.result.outcome == "unknown"
            else "Помощник недоступен"
        )
        return prefix + (" — текст в буфере" if self.copied else " — не удалось скопировать текст")

    @property
    def detail(self) -> str:
        if self.result.outcome == "unknown":
            return (
                "Помощник не ответил вовремя. Возможно, команда всё же принята — "
                "проверьте в Astra Cowork, прежде чем повторять"
            )
        details = {
            "model_untrusted": "Выберите модель из каталога программы",
            "not_running": "Astra Cowork не запущен",
            "no_contract": "Помощник не поддерживает голосовые команды — обновите Astra Cowork",
            "no_bus": "Не удалось связаться с помощником на этом компьютере",
            "busy": "Помощник сейчас занят. Попробуйте ещё раз через несколько секунд",
            "expired": "Помощник не успел принять команду. Попробуйте ещё раз",
            "policy_disabled": "Приём голосовых команд выключен в настройках Astra Cowork",
            "not_ready": "Помощник ещё не готов. Попробуйте ещё раз через несколько секунд",
            "session_unsupported": "Во Fly приём голосовых команд пока не поддерживается",
        }
        return details.get(
            self.result.server_reason, details.get(self.result.reason, "Помощник отклонил команду")
        )


class CommandMode:
    def __init__(
        self,
        *,
        client: CommandClient,
        session: Callable[[], SessionSnapshot],
        model_trusted: Callable[[], bool],
        publish: Callable[[str], bool],
        present: Callable[[CommandFeedback], None],
    ) -> None:
        self.client = client
        self.session = session
        self.model_trusted = model_trusted
        self.publish = publish
        self.present = present
        self.last_text: str | None = None
        self.last_feedback: CommandFeedback | None = None
        self._generation = 0
        self._pending = False
        self._capture_epoch: tuple[int, int] | None = None

    @property
    def allowed(self) -> bool:
        return self.session().allowed

    @property
    def publication_allowed(self) -> bool:
        snapshot = self.session()
        return snapshot.allowed and (
            self._capture_epoch is None or snapshot.blocked_epoch == self._capture_epoch[0]
        )

    def begin(self) -> None:
        snapshot = self.session()
        self._capture_epoch = (snapshot.blocked_epoch, snapshot.sleep_epoch)

    def remember(self, text: str) -> str:
        """Remember the current phrase before preview, without publishing it."""
        self.last_text = normalize_text(text)
        return self.last_text

    def deliver(self, text: str, callback: Callable[[DeliveryResult], None] | None = None) -> bool:
        if self._pending:
            return False
        self.remember(text)
        self._generation += 1
        generation = self._generation
        self._pending = True
        snapshot = self.session()
        capture_epoch = self._capture_epoch or (snapshot.blocked_epoch, snapshot.sleep_epoch)

        def finish(result: DeliveryResult) -> None:
            if generation != self._generation or not self._pending:
                return
            self._pending = False
            copied = None
            current = self.session()
            same_epoch = current.blocked_epoch == capture_epoch[0]
            if current.sleep_epoch != capture_epoch[1]:
                result = DeliveryResult(
                    "unknown" if result.outcome != "undelivered" else "undelivered", "suspended"
                )
            if self.allowed and same_epoch:
                if result.outcome != "delivered":
                    copied = self.recover()
                feedback = CommandFeedback(result, copied)
                self.last_feedback = feedback
                if self.allowed:
                    self.present(feedback)
                if copied is False:
                    result = DeliveryResult(
                        result.outcome, "clipboard_failed", result.error_name, result.server_reason
                    )
            if callback is not None:
                callback(result)

        if not self.model_trusted():
            finish(DeliveryResult("undelivered", "model_untrusted"))
        elif not self.allowed or self.session().blocked_epoch != capture_epoch[0]:
            reason = (
                "suspended"
                if self.session().preparing_for_sleep
                or self.session().sleep_epoch != capture_epoch[1]
                else "locked"
            )
            finish(DeliveryResult("undelivered", reason))
        else:
            self.client.submit(self.last_text or "", finish)
        return True

    def recover(self) -> bool:
        """Explicit copy after unlock, without retrying Submit or auto-publication."""
        if not self.allowed or self.last_text is None:
            return False
        try:
            return self.publish(self.last_text) is True
        except Exception:
            return False

    def session_changed(self, transition: SessionSnapshot | None = None) -> None:
        # Lock hides presentation at outcome, but does not claim a sent request was
        # undone. Sleep invalidates its generation through the client's single callback.
        snapshot = transition if transition is not None else self.session()
        if snapshot.preparing_for_sleep:
            self.client.cancel()
