import json
import logging
import ssl
from pathlib import Path
from gigachat import GigaChat
from config import CERT_PATH

class GigaChatHelper:
    def __init__(self):
        self.client = self._init_client()
    
    def _init_client(self):
        """Инициализация клиента GigaChat с сертификатами"""
        try:
            # Проверяем существование сертификатов
            cert_files = {
                "ca_bundle_file": CERT_PATH / "ca.pem",
                "cert_file": CERT_PATH / "tls.pem",
                "key_file": CERT_PATH / "tls.key"
            }
            
            for name, path in cert_files.items():
                if not path.exists():
                    logging.error(f"Сертификат не найден: {path}")
                    return None
            
            # Создаем SSL контекст с сертификатами
            context = ssl.create_default_context()
            context.load_cert_chain(
                certfile=str(cert_files["cert_file"]),
                keyfile=str(cert_files["key_file"])
            )
            context.load_verify_locations(cafile=str(cert_files["ca_bundle_file"]))
            
            
            return GigaChat(
                base_url="https://gigachat-ift.sberdevices.delta.sbrf.ru/v1",
                ca_bundle_file=str(cert_files["ca_bundle_file"]),
                cert_file=str(cert_files["cert_file"]),
                key_file=str(cert_files["key_file"]),
                model='GigaChat-2-Pro',
                scope='GIGACHAT_API_CORP',
                timeout=600,
                verbose=True,
                verify_ssl=True
            )
        except Exception as e:
            logging.error(f"Ошибка инициализации GigaChat: {str(e)}")
            return None

    def generate_recommendations(self, check_results, dist_info):
        """Генерация рекомендаций с помощью GigaChat"""
        try:
            if not self.client:
                return "GigaChat недоступен. Вот сырые данные:\n\nПроверка документов:\n" + json.dumps(check_results, ensure_ascii=False, indent=2) + "\n\nДистрибутивы:\n" + json.dumps(dist_info, ensure_ascii=False, indent=2)
            
            # Формируем промпт (точно как в оригинале)
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
                dist_info=json.dumps(dist_info, ensure_ascii=False, indent=2)
            )
            
            # Вызов GigaChat
            response = self.client.chat(prompt)
            return response.choices[0].message.content
            
        except Exception as e:
            logging.error(f"Ошибка при генерации рекомендаций: {str(e)}")
            return f"Ошибка: {str(e)}\n\nСырые данные:\nПроверка: {json.dumps(check_results, ensure_ascii=False)}\nДистрибутивы: {json.dumps(dist_info, ensure_ascii=False)}"

# Глобальный экземпляр помощника GigaChat (инициализируется при импорте модуля)
GIGA_HELPER = GigaChatHelper()