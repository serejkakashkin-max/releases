"""Lazy, feature-controlled access to the optional GigaChat integration."""

from __future__ import annotations

import json
import logging
import ssl
import threading
from typing import Any

from config import CERT_PATH
from services.feature_flags_service import is_gigachat_enabled


class GigaChatDisabledError(RuntimeError):
    """Raised when a new GigaChat call is blocked by the feature switch."""


class GigaChatUnavailableError(RuntimeError):
    """Raised when the enabled integration cannot initialize or answer."""


class GigaChatHelper:
    """Owns one lazily-created SDK client without import-time side effects."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._client: Any = None
        self._state = "uninitialized"
        self._active_calls: dict[Any, int] = {}
        self._retired_clients: set[Any] = set()

    @property
    def client(self) -> Any:
        """Return an already initialized client without causing initialization."""
        if not self.is_enabled():
            self.sync_runtime_state()
            return None
        with self._lock:
            return self._client

    def is_enabled(self) -> bool:
        return is_gigachat_enabled()

    def get_status(self) -> dict[str, Any]:
        enabled = self.is_enabled()
        with self._lock:
            state = self._state
        if not enabled:
            state = "disabled"
        return {
            "enabled": enabled,
            "available": enabled and state == "ready",
            "state": state,
        }

    def _build_client(self) -> Any:
        # Importing the optional SDK and touching certificates are deliberately
        # inside the enabled, first-call boundary.
        from gigachat import GigaChat

        cert_files = {
            "ca_bundle_file": CERT_PATH / "ca.pem",
            "cert_file": CERT_PATH / "tls.pem",
            "key_file": CERT_PATH / "tls.key",
        }
        missing = [path.name for path in cert_files.values() if not path.is_file()]
        if missing:
            raise GigaChatUnavailableError("Сертификаты GigaChat недоступны.")

        context = ssl.create_default_context()
        context.load_cert_chain(
            certfile=str(cert_files["cert_file"]),
            keyfile=str(cert_files["key_file"]),
        )
        context.load_verify_locations(cafile=str(cert_files["ca_bundle_file"]))

        return GigaChat(
            base_url="https://gigachat-ift.sberdevices.delta.sbrf.ru/v1",
            ca_bundle_file=str(cert_files["ca_bundle_file"]),
            cert_file=str(cert_files["cert_file"]),
            key_file=str(cert_files["key_file"]),
            model="GigaChat-2-Pro",
            scope="GIGACHAT_API_CORP",
            timeout=600,
            verbose=True,
            verify_ssl_certs=True,
        )

    def _get_or_create_client(self) -> Any:
        if not self.is_enabled():
            self.sync_runtime_state()
            raise GigaChatDisabledError("GigaChat отключён администратором.")
        with self._lock:
            if self._client is not None:
                return self._client
            if self._state == "unavailable":
                raise GigaChatUnavailableError("GigaChat временно недоступен.")
            try:
                self._client = self._build_client()
                self._state = "ready"
                return self._client
            except GigaChatUnavailableError:
                self._state = "unavailable"
                raise
            except Exception as exc:
                self._state = "unavailable"
                logging.error("Ошибка инициализации GigaChat: %s", exc)
                raise GigaChatUnavailableError("GigaChat временно недоступен.") from exc

    @staticmethod
    def _close_client(client: Any) -> None:
        close = getattr(client, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception as exc:
            logging.warning("Не удалось закрыть клиент GigaChat: %s", exc)

    def sync_runtime_state(self) -> None:
        """Apply an OFF transition without interrupting an in-flight request."""
        if self.is_enabled():
            with self._lock:
                if self._state == "disabled":
                    self._state = "uninitialized"
            return

        close_now = None
        with self._lock:
            client = self._client
            self._client = None
            self._state = "disabled"
            if client is not None:
                if self._active_calls.get(client, 0):
                    self._retired_clients.add(client)
                else:
                    close_now = client
        if close_now is not None:
            self._close_client(close_now)

    def chat(self, prompt: str) -> Any:
        with self._lock:
            client = self._get_or_create_client()
            self._active_calls[client] = self._active_calls.get(client, 0) + 1
        try:
            return client.chat(prompt)
        finally:
            close_retired = False
            with self._lock:
                remaining = self._active_calls.get(client, 1) - 1
                if remaining > 0:
                    self._active_calls[client] = remaining
                else:
                    self._active_calls.pop(client, None)
                    if client in self._retired_clients:
                        self._retired_clients.discard(client)
                        close_retired = True
            if close_retired:
                self._close_client(client)

    def reset_for_tests(self) -> None:
        clients = []
        with self._lock:
            if self._client is not None:
                clients.append(self._client)
            clients.extend(self._retired_clients)
            self._client = None
            self._state = "uninitialized"
            self._active_calls.clear()
            self._retired_clients.clear()
        for client in clients:
            self._close_client(client)

    def generate_recommendations(self, check_results, dist_info):
        prompt = """
Ты профессиональный аналитик релизов ПО. У тебя есть данные о проверке документов и дистрибутивах для вывода из эксплуатации.

Данные проверки документов:
{check_results}

Данные дистрибутивов:
{dist_info}

Сформируй отчет-рекомендацию строго в следующем формате. Не добавляй ничего лишнего, следуй структуре точно:

# Проверка документов

- Если нет ошибок: [color=00ff00]Все документы проверены успешно.[/color]

- Если есть ошибки: Перечисли файлы с ошибками, для каждого файла - список ошибок в красном, сгруппированные по типам. Затем дай советы по исправлению в отдельном абзаце под заголовком "Рекомендации:".

# Рекомендации по дистрибутивам

Доступные дистрибутивы (отсортированы по дате, свежие сверху):

- Перечисли каждый дистрибутив в формате: - [dist name]

Количество дистрибутивов: [count]

Рекомендуем вывести:

- Если <=3: Нет дистрибутивов, подлежащих выводу.

- Если >3: Перечисли старые дистрибутивы в красном, рекомендуй вывести старые, оставив 3 свежих.  [color=ff0000]- [dist name][/color]

Оставить актуальные версии:

- Перечисли свежие 3 в зеленом: [color=00ff00]- [dist name][/color]

# Заключение

Сформируй краткое заключение на основе всех данных, включая проверку документов и рекомендации по дистрибутивам.

Используй только указанный markdown и цвета. Сделай кратко и информативно.
""".format(
            check_results=json.dumps(check_results, ensure_ascii=False, indent=2),
            dist_info=json.dumps(dist_info, ensure_ascii=False, indent=2),
        )
        try:
            response = self.chat(prompt)
            return response.choices[0].message.content
        except GigaChatDisabledError:
            return "GigaChat отключён администратором."
        except GigaChatUnavailableError:
            return "GigaChat временно недоступен."
        except Exception as exc:
            logging.error("Ошибка при генерации рекомендаций: %s", exc)
            return "Не удалось получить рекомендации GigaChat."


GIGA_HELPER = GigaChatHelper()
