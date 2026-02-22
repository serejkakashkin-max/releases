from flask import Blueprint, render_template
from services.counter_service import get_stats
from config import VERSION, VERSION_HISTORY  # НОВОЕ: импорт версии

main_bp = Blueprint('main', __name__)

@main_bp.route('/')
def index():
    stats = get_stats()
    return render_template('index.html', stats=stats, version=VERSION, version_history=VERSION_HISTORY)