import sys
from pathlib import Path
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv(override=True)

# Добавляем текущую директорию в путь для импортов
script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(script_dir))

# Импортируем расширения и конфигурацию
from extensions import app
from routes import register_routes

# Регистрируем все маршруты
register_routes(app)

if __name__ == '__main__':
    app.run(debug=True, port=5001)