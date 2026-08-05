import logging
import sys
from importlib.metadata import version as get_version
from packaging.version import Version
from datetime import datetime, timedelta

# Настраиваем логгер
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

# Минимальная рекомендуемая версия holidays
MINIMUM_HOLIDAYS_VERSION = "0.90.0"
# Дата, после которой рекомендуется обновление
RECOMMENDED_UPDATE_INTERVAL_DAYS = 90  # 3 месяца


def check_holidays_version() -> dict:
    """
    Проверяет версию библиотеки holidays и возвращает результат проверки.
    """
    result = {
        "installed": False,
        "version": None,
        "is_up_to_date": False,
        "needs_update": False,
        "message": "",
    }

    try:
        # Проверяем, установлена ли библиотека
        import holidays
        installed_version = get_version("holidays")
        result["installed"] = True
        result["version"] = installed_version

        # Проверяем, соответствует ли минимальной версии
        try:
            current = Version(installed_version)
            minimum = Version(MINIMUM_HOLIDAYS_VERSION)
            result["is_up_to_date"] = current >= minimum
            result["needs_update"] = current < minimum
        except Exception:
            # Если не удалось сравнить версии, считаем, что всё ок
            result["is_up_to_date"] = True
            result["needs_update"] = False

        # Формируем сообщение
        if result["needs_update"]:
            result["message"] = (
                f"Установлена старая версия holidays ({installed_version}). "
                f"Рекомендуется обновить до {MINIMUM_HOLIDAYS_VERSION} или новее. "
                f"Команда: pip install --upgrade holidays"
            )
        else:
            result["message"] = f"Версия holidays ({installed_version}) актуальна."

    except ImportError:
        result["message"] = (
            "Библиотека holidays не установлена! "
            "Праздники не будут загружаться. "
            "Установите: pip install holidays"
        )
    except Exception as e:
        result["message"] = f"Ошибка проверки версии holidays: {e}"
        result["installed"] = False

    return result


def check_holidays_data_year(year: int) -> dict:
    """
    Проверяет, есть ли в библиотеке данные для указанного года.
    """
    result = {
        "has_data": False,
        "year": year,
        "holidays_count": 0,
        "message": "",
    }

    try:
        import holidays
        ru_holidays = holidays.Russia(years=year)
        count = len(ru_holidays)
        result["has_data"] = True
        result["holidays_count"] = count

        if count == 0:
            result["message"] = f"Для года {year} нет праздников. Возможно, данные не обновлены."
        else:
            result["message"] = f"Для года {year} найдено {count} праздничных дней."

    except ImportError:
        result["message"] = "Библиотека holidays не установлена."
    except Exception as e:
        result["message"] = f"Ошибка проверки данных для {year}: {e}"

    return result


def log_holidays_status(logger_instance=None):
    """
    Логирует статус библиотеки holidays при запуске.
    """
    if logger_instance is None:
        logger_instance = logger

    logger_instance.info("=" * 60)
    logger_instance.info("ПРОВЕРКА БИБЛИОТЕКИ HOLIDAYS")
    logger_instance.info("=" * 60)

    # Проверяем версию
    version_check = check_holidays_version()
    logger_instance.info(f"Статус: {'✅ Установлена' if version_check['installed'] else '❌ НЕ УСТАНОВЛЕНА'}")
    if version_check['version']:
        logger_instance.info(f"Версия: {version_check['version']}")

    if version_check['needs_update']:
        logger_instance.warning("⚠️ " + version_check['message'])
    elif version_check['installed']:
        logger_instance.info("✅ " + version_check['message'])
    else:
        logger_instance.error("❌ " + version_check['message'])

    # Проверяем данные для текущего и следующего года
    current_year = datetime.now().year
    for year in [current_year, current_year + 1]:
        data_check = check_holidays_data_year(year)
        if data_check['has_data']:
            logger_instance.info(f"  {year}: {data_check['holidays_count']} праздников")
            if data_check['holidays_count'] == 0:
                logger_instance.warning(f"⚠️ Для {year} нет данных. Рекомендуется обновить holidays.")
        else:
            logger_instance.warning(f"⚠️ {data_check['message']}")

    # Проверяем, не пора ли обновить библиотеку
    try:
        # Проверяем дату последней модификации файла holidays
        import holidays
        import os
        holidays_file = holidays.__file__
        if os.path.exists(holidays_file):
            mtime = datetime.fromtimestamp(os.path.getmtime(holidays_file))
            days_since_update = (datetime.now() - mtime).days
            if days_since_update > RECOMMENDED_UPDATE_INTERVAL_DAYS:
                logger_instance.warning(
                    f"⚠️ Библиотека holidays не обновлялась более {days_since_update} дней. "
                    f"Рекомендуется обновить: pip install --upgrade holidays"
                )
    except Exception:
        pass

    logger_instance.info("=" * 60)