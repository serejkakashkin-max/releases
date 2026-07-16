from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple


MESSAGE_TITLES = {
    "info": "Статус",
    "success": "Готово",
    "warning": "Внимание",
    "error": "Ошибка",
}


@dataclass(frozen=True)
class UserMessage:
    kind: str
    text: str
    title: str = ""
    details: Tuple[str, ...] = ()

    @property
    def resolved_title(self) -> str:
        return self.title or MESSAGE_TITLES.get(self.kind, "Сообщение")


def build_user_messages(
    message: Optional[str] = None,
    error: Optional[str] = None,
    warning: Optional[str] = None,
    info: Optional[str] = None,
) -> List[UserMessage]:
    messages: List[UserMessage] = []
    if error:
        messages.append(UserMessage(kind="error", text=error))
    if warning:
        messages.append(UserMessage(kind="warning", text=warning))
    if message:
        messages.append(UserMessage(kind="success", text=message))
    if info:
        messages.append(UserMessage(kind="info", text=info))
    return messages


def has_user_messages(messages: Iterable[UserMessage]) -> bool:
    return any(True for _ in messages)
