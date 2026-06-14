from flask import Blueprint, render_template
from services.counter_service import get_stats
from services.feature_flags_service import is_maintenance_enabled
from config import VERSION, VERSION_HISTORY  # НОВОЕ: импорт версии

main_bp = Blueprint('main', __name__)

@main_bp.route('/')
def index():
    stats = get_stats()
    return render_template(
        'index.html',
        stats=stats,
        version=VERSION,
        version_history=VERSION_HISTORY,
        maintenance_enabled=is_maintenance_enabled("index"),
        maintenance_scope="index",
        maintenance_title="Главная страница на обслуживании",
        chatbot_maintenance=is_maintenance_enabled("chatbot"),
    )


@main_bp.route('/help')
def help_page():
    return render_template('help.html')
