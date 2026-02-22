import json
import logging
import sys
from pathlib import Path
from version import VERSION, VERSION_HISTORY

# --- Базовые пути ---
SCRIPT_DIR = Path(__file__).resolve().parent
DOC_TEMPLATES_ROOT = SCRIPT_DIR / "doc_templates"
SMS_TEMPLATES_ROOT = SCRIPT_DIR / "sms_templates"
LOG_FILE = SCRIPT_DIR / "logs" / "release_generator.log"
SMS_LOG_FILE = SCRIPT_DIR / "logs" / "sms_generator.log"
CONFIG_FILE = SCRIPT_DIR / "config.json"
CERT_PATH = SCRIPT_DIR / "certificates"
COUNTERS_FILE = SCRIPT_DIR / "data" / "counters.json"

# Создаем необходимые папки
Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
CERT_PATH.mkdir(parents=True, exist_ok=True)
COUNTERS_FILE.parent.mkdir(parents=True, exist_ok=True)

# --- Константы ---
DEFAULT_BH_PLAYBOOKS = [
    "DB_UPDATE", "OPENSHIFT_PURGE_PROJECT", "NGINX_DEPLOY", "KAFKA_UPDATE_FP",
    "MIGRATION_FP_CONF", "CLEANUP_FP_CONFIG", "DRY_RUN", "OPENSHIFT_UPDATE_REPLICAS",
    "OPENSHIFT_INGRESS_EGRESS_DEPLOY", "OPENSHIFT_UPDATE_ISTIO_REPLICAS",
    "OPENSHIFT_EXTRACT_DUMP", "WAS_FPI_UTIL_JOB_CONF", "DEBUG", "OPENSHIFT_DEPLOY",
    "IMPORT_SUP_PARAMS", "IMPORT_TENGRI_PARAMS", "IMPORT_SECURITY_PARAMS",
    "IMPORT_DICTIONARY_PARAMS", "IMPORT_LOGGER_PARAMS"
]

OPLOT_VALUES = [
    "Кондратьева А.А.", "Тутов А.М.", "Частухин А.М.",
    "Ефимов В.В.", "Гапоненко Д.А.", "Фисан К.Ю.", "Глотов К.С.",
    "Мухиддинов М.Б.", "Кашкин С.Н."
]

# === Константы для Дашборда дежурного ===
# Список дежурных ОПЛОТ (ФИО полностью как в Jira)
DASHBOARD_ASSIGNEES = [
    "Гапоненко Дмитрий Анатольевич",
    "Глотов Кирилл Сергеевич", 
    "Ефимов Владимир Владимирович",
    "Кашкин Сергей Николаевич",
    "Кондратьева Алена Александровна",
    "Мухиддинов Манучехр Бахриддинович",
    "Сафонов Кирилл Евгеньевич",
    "Тутов Артем Михайлович",
    "Фисан Кирилл Юрьевич",
    "Частухин Александр Михайлович",
    "Андреев Василий Юрьевич"
]

# Теги для фильтрации задач в Jira
DASHBOARD_TAG = "СУП"
DASHBOARD_TAG_VNEDRENIE = "Внедрение"  # НОВОЕ: тег для внедрения

# Период в днях для поиска задач
DASHBOARD_DAYS_BACK = 30

# Интервал обновления кэша в секундах (1 час = 3600 секунд)
DASHBOARD_CACHE_TTL = 3600

# --- Загрузка токенов ---
try:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        TOKENS = json.load(f)
except Exception as e:
    logging.error(f"Ошибка загрузки config.json: {e}")
    sys.exit(1)

# --- Настройка логирования ---
def setup_logging():
    logging.basicConfig(
        filename=LOG_FILE,
        filemode='w',
        format='[%(asctime)s] %(levelname)s - %(message)s',
        level=logging.DEBUG
    )
    
    sms_logger = logging.getLogger('sms')
    sms_handler = logging.FileHandler(SMS_LOG_FILE, mode='w')
    sms_handler.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s - %(message)s'))
    sms_logger.addHandler(sms_handler)
    sms_logger.setLevel(logging.DEBUG)
    return sms_logger

# Экспортируем логгер SMS
sms_logger = setup_logging()