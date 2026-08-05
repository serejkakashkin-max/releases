import sys
import os
import logging
from pathlib import Path
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv(override=True)


def _is_quiet_flask_cli():
    """Keep the local ``flask run`` console compact without affecting WSGI logging."""
    return (
        os.environ.get("FLASK_RUN_FROM_CLI", "").strip().lower() == "true"
        and os.environ.get("OPLOT_FLASK_CONSOLE_VERBOSE", "").strip().lower()
        not in {"1", "true", "yes", "on"}
    )


def _configure_local_flask_console():
    if not _is_quiet_flask_cli():
        return

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.CRITICAL)
    if not root_logger.handlers:
        # Prevent imported optional modules from configuring the application-wide
        # root logger as a side effect of ``logging.basicConfig``.
        root_logger.addHandler(logging.NullHandler())
    logging.getLogger("werkzeug").setLevel(logging.ERROR)


_configure_local_flask_console()

# Добавляем текущую директорию в путь для импортов
script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(script_dir))

# 🔥 ПРОВЕРКА БИБЛИОТЕКИ HOLIDAYS (только один раз, даже в debug режиме)
if not os.environ.get('WERKZEUG_RUN_MAIN'):
    try:
        from VA.schedule_manager.utils.version_checker import log_holidays_status
        if _is_quiet_flask_cli():
            holidays_logger = logging.getLogger("oplot.startup.holidays")
            holidays_logger.addHandler(logging.NullHandler())
            holidays_logger.propagate = False
            log_holidays_status(holidays_logger)
        else:
            log_holidays_status()
    except ImportError:
        logging.getLogger(__name__).warning(
            "Модуль version_checker не найден, "
            "пропускаем проверку holidays"
        )
    except Exception as e:
        logging.getLogger(__name__).warning("Ошибка проверки holidays: %s", e)

# Импортируем расширения и конфигурацию
from extensions import app
from routes import register_routes

# Регистрируем все маршруты
register_routes(app)

if __name__ == '__main__':
    app.run(debug=True, port=5001)
