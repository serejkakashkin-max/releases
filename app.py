import sys
import os
from pathlib import Path
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv(override=True)

# Добавляем текущую директорию в путь для импортов
script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(script_dir))

# 🔥 ПРОВЕРКА БИБЛИОТЕКИ HOLIDAYS (только один раз, даже в debug режиме)
if not os.environ.get('WERKZEUG_RUN_MAIN'):
    try:
        from VA.schedule_manager.utils.version_checker import log_holidays_status
        log_holidays_status()
    except ImportError:
        print("⚠️ Модуль version_checker не найден, пропускаем проверку holidays")
    except Exception as e:
        print(f"⚠️ Ошибка проверки holidays: {e}")

# Импортируем расширения и конфигурацию
from extensions import app
from routes import register_routes

# Регистрируем все маршруты
register_routes(app)

if __name__ == '__main__':
    app.run(debug=True, port=5001)