"""
Сервис для генерации отчётов по сотрудникам.
Создаёт красивый HTML отчёт с диаграммами для руководителей.
"""

import json
import html
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Dict, List, Any
from collections import defaultdict

import requests
from config import DASHBOARD_ASSIGNEES, DASHBOARD_DAYS_BACK, get_dashboard_assignee_display_name
from services.dashboard_service import get_jira_domain_and_token

# Папка для хранения отчётов
REPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'reports')


class ReportService:
    """Сервис генерации отчётов по сотрудникам"""
    
    def __init__(self):
        self.assignees = DASHBOARD_ASSIGNEES
        self.days_back = DASHBOARD_DAYS_BACK
    
    def generate_assignee_report(self, days: int = 30, quarter: int = None, year: int = None) -> Dict[str, Any]:
        """
        Генерирует отчёт по закрытым задачам сотрудников за указанный период.
        
        Args:
            days: Количество дней для анализа (по умолчанию 30)
            quarter: Квартал (1-4) - если указан, используется вместо days
            year: Год для квартала (по умолчанию текущий)
            
        Returns:
            Dict с данными отчёта
        """
        # Определяем период
        if quarter and 1 <= quarter <= 4:
            # Используем квартал
            start_date, end_date = self._get_quarter_dates(quarter, year)
            period_type = f"{quarter} квартал {start_date.year}"
            days = (end_date - start_date).days
        else:
            # Используем количество дней
            end_date = datetime.now()
            start_date = end_date - timedelta(days=days)
            period_type = f"последние {days} дней"
        
        logging.info(f"[REPORT] Генерация отчёта ({period_type}): с {start_date.date()} по {end_date.date()}")
        
        # Получаем данные из Jira
        tasks = self._fetch_closed_tasks(start_date, end_date)
        
        # Анализируем статистику
        stats = self._analyze_statistics(tasks)
        
        return {
            'period': {
                'start': start_date.strftime('%Y-%m-%d'),
                'end': end_date.strftime('%Y-%m-%d'),
                'days': days,
                'quarter': quarter,
                'year': year if quarter else None,
                'period_type': period_type
            },
            'statistics': stats,
            'tasks': tasks,
            'total_tasks': len(tasks),
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
    
    def _get_quarter_dates(self, quarter: int, year: int = None) -> tuple:
        """
        Возвращает даты начала и конца квартала.
        
        Args:
            quarter: Номер квартала (1-4)
            year: Год (по умолчанию текущий)
            
        Returns:
            tuple: (start_date, end_date)
        """
        if year is None:
            year = datetime.now().year
        
        quarter_months = {
            1: (1, 3),    # Янв - Мар
            2: (4, 6),    # Апр - Июн
            3: (7, 9),    # Июл - Сен
            4: (10, 12)   # Окт - Дек
        }
        
        start_month, end_month = quarter_months[quarter]
        
        # Начало квартала (первый день первого месяца)
        start_date = datetime(year, start_month, 1)
        
        # Конец квартала (последний день последнего месяца)
        if end_month == 12:
            end_date = datetime(year, 12, 31, 23, 59, 59)
        else:
            # Следующий месяц минус 1 день
            next_month = end_month + 1
            next_month_date = datetime(year, next_month, 1)
            end_date = next_month_date - timedelta(days=1)
            end_date = end_date.replace(hour=23, minute=59, second=59)
        
        return start_date, end_date
    
    def _fetch_closed_tasks(self, start_date: datetime, end_date: datetime) -> List[Dict]:
        """Получает закрытые задачи из Jira за период"""
        try:
            domain, token = get_jira_domain_and_token()
            
            # Формируем JQL запрос
            assignees_filter = ', '.join([f'"{name}"' for name in self.assignees])
            start_str = start_date.strftime('%Y-%m-%d')
            end_str = end_date.strftime('%Y-%m-%d')
            
            jql = (
                f'project = OPLOT AND '
                f'assignee IN ({assignees_filter}) AND '
                f'status IN (Done, Closed, Resolved) AND '
                f'updated >= "{start_str} 00:00" AND updated <= "{end_str} 23:59" '
                f'ORDER BY updated DESC'
            )
            
            logging.info(f"[REPORT] JQL: {jql}")
            
            url = f"{domain}/rest/api/2/search"
            headers = {"Authorization": f"Bearer {token}"}
            params = {
                'jql': jql,
                'maxResults': 1000,
                'fields': 'key,summary,created,updated,status,assignee,reporter,labels,priority,issuetype,resolutiondate'
            }
            
            response = requests.get(url, headers=headers, params=params, verify=False, timeout=60)
            response.raise_for_status()
            
            data = response.json()
            issues = data.get('issues', [])
            
            tasks = []
            for issue in issues:
                assignee = issue['fields'].get('assignee')
                labels = issue['fields'].get('labels', [])
                
                tasks.append({
                    'key': issue['key'],
                    'summary': issue['fields'].get('summary', ''),
                    'status': issue['fields'].get('status', {}).get('name', ''),
                    'assignee_name': get_dashboard_assignee_display_name(assignee.get('displayName', '?? ????????') if assignee else '?? ????????'),
                    'assignee_email': assignee.get('emailAddress', '') if assignee else '',
                    'created': issue['fields'].get('created', ''),
                    'updated': issue['fields'].get('updated', ''),
                    'resolutiondate': issue['fields'].get('resolutiondate', ''),
                    'priority': issue['fields'].get('priority', {}).get('name', ''),
                    'labels': labels,  # Все теги задачи
                    'first_label': labels[0] if labels else '',
                    'url': f"{domain}/browse/{issue['key']}"
                })
            
            logging.info(f"[REPORT] Получено {len(tasks)} закрытых задач")
            return tasks
            
        except Exception as e:
            logging.error(f"[REPORT] Ошибка получения задач: {e}")
            return []
    
    def _analyze_statistics(self, tasks: List[Dict]) -> Dict[str, Any]:
        """Анализирует статистику по сотрудникам - собирает ВСЕ теги"""
        
        # Собираем все уникальные теги из задач
        all_tags = set()
        for task in tasks:
            for label in task.get('labels', []):
                all_tags.add(label.lower())
        
        # Сортируем теги для консистентности
        sorted_tags = sorted(all_tags)
        
        # Статистика по сотрудникам - динамически для всех тегов
        assignee_stats = defaultdict(lambda: {
            'total': 0,
            'tags': {},  # Динамический словарь для всех тегов
            'no_tags': 0,
            'tasks': []
        })
        
        # Общая статистика по тегам - динамически
        tag_totals = {}
        
        for task in tasks:
            assignee = task['assignee_name']
            assignee_stats[assignee]['total'] += 1
            assignee_stats[assignee]['tasks'].append(task)
            
            labels = task.get('labels', [])
            
            # Подсчёт только по первому тегу (как указано в требованиях)
            if labels:
                first_label = labels[0].lower()  # Берем только первый тег
                # Считаем для сотрудника
                if first_label not in assignee_stats[assignee]['tags']:
                    assignee_stats[assignee]['tags'][first_label] = 0
                assignee_stats[assignee]['tags'][first_label] += 1

                # Считаем общую статистику
                if first_label not in tag_totals:
                    tag_totals[first_label] = 0
                tag_totals[first_label] += 1
            else:
                # Задачи без тегов
                assignee_stats[assignee]['no_tags'] += 1
        
        # Добавляем счётчик задач без тегов в общую статистику
        no_tags_total = sum(s['no_tags'] for s in assignee_stats.values())
        if no_tags_total > 0:
            tag_totals['(без тега)'] = no_tags_total
        
        # Сортируем сотрудников по количеству закрытых задач
        sorted_assignees = sorted(
            assignee_stats.items(),
            key=lambda x: x[1]['total'],
            reverse=True
        )
        
        return {
            'by_assignee': dict(sorted_assignees),
            'tag_totals': tag_totals,
            'all_tags': sorted_tags,  # Список всех тегов для отображения
            'assignee_count': len(assignee_stats),
            'avg_per_assignee': round(len(tasks) / len(assignee_stats), 1) if assignee_stats else 0
        }
    
    def _generate_chart_colors(self, count: int) -> List[str]:
        """Генерирует цвета для диаграмм"""
        # Палитра цветов
        colors = [
            '#ef5350', '#42a5f5', '#ab47bc', '#66bb6a', '#ffa726',
            '#26c6da', '#ec407a', '#7e57c2', '#ff7043', '#9ccc65',
            '#5c6bc0', '#26a69a', '#ffca28', '#ef5350', '#8d6e63',
            '#78909c', '#bdbdbd', '#8e24aa', '#d4e157', '#ff8a65'
        ]
        # Если нужно больше цветов, повторяем палитру
        result = []
        for i in range(count):
            result.append(colors[i % len(colors)])
        return result
    
    def generate_html_report(self, report_data: Dict[str, Any]) -> str:
        """
        Генерирует HTML отчёт с диаграммами.
        
        Args:
            report_data: Данные отчёта от generate_assignee_report
            
        Returns:
            HTML строка
        """
        stats = report_data['statistics']
        period = report_data['period']
        
        # Подготовка данных для диаграмм
        assignee_names = list(stats['by_assignee'].keys())
        assignee_totals = [data['total'] for data in stats['by_assignee'].values()]
        
        # Данные для круговой диаграммы (по всем тегам динамически)
        # Сортируем теги по количеству (по убыванию)
        sorted_tags = sorted(stats['tag_totals'].items(), key=lambda x: x[1], reverse=True)
        tag_labels = [tag.capitalize() if tag != '(без тега)' else tag for tag, _ in sorted_tags]
        tag_values = [count for _, count in sorted_tags]
        
        # Цвета для диаграммы (генерируем динамически)
        tag_colors = self._generate_chart_colors(len(tag_labels))
        
        # Генерируем HTML таблицы динамически на основе всех тегов
        table_headers = self._generate_table_headers(stats['all_tags'])
        table_rows = self._generate_table_rows(stats['by_assignee'], stats['all_tags'])
        
        # Генерируем HTML
        html = f'''<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Отчёт по дежурным - {period['start']} / {period['end']}</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            padding: 20px;
            min-height: 100vh;
        }}
        
        .container {{
            max-width: 1400px;
            margin: 0 auto;
        }}
        
        .header {{
            background: white;
            border-radius: 16px;
            padding: 30px;
            margin-bottom: 20px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.1);
            text-align: center;
        }}
        
        .header h1 {{
            color: #333;
            font-size: 32px;
            margin-bottom: 10px;
        }}
        
        .header .subtitle {{
            color: #666;
            font-size: 18px;
        }}
        
        .header .period {{
            color: #888;
            font-size: 14px;
            margin-top: 10px;
        }}
        
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 20px;
        }}
        
        .stat-card {{
            background: white;
            border-radius: 16px;
            padding: 25px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.1);
            text-align: center;
        }}
        
        .stat-card .number {{
            font-size: 48px;
            font-weight: 700;
            color: #667eea;
            margin-bottom: 5px;
        }}
        
        .stat-card .label {{
            color: #666;
            font-size: 16px;
        }}
        
        .charts-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
            gap: 20px;
            margin-bottom: 20px;
        }}
        
        .chart-card {{
            background: white;
            border-radius: 16px;
            padding: 25px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.1);
        }}
        
        .chart-card h3 {{
            color: #333;
            margin-bottom: 20px;
            font-size: 20px;
        }}
        
        .chart-container {{
            position: relative;
            height: 300px;
        }}
        
        .table-card {{
            background: white;
            border-radius: 16px;
            padding: 25px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.1);
            overflow-x: auto;
        }}
        
        .table-card h3 {{
            color: #333;
            margin-bottom: 20px;
            font-size: 20px;
        }}

        .section-spacing {{
            margin-top: 20px;
        }}
        
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
        }}
        
        th, td {{
            padding: 10px;
            text-align: left;
            border-bottom: 1px solid #eee;
        }}
        
        th {{
            background: #f8f9fa;
            font-weight: 600;
            color: #333;
            position: sticky;
            top: 0;
        }}
        
        tr:hover {{
            background: #f8f9fa;
        }}

        .assignee-row {{
            cursor: pointer;
        }}

        .assignee-row:hover {{
            background: #eef2ff;
        }}

        .assignee-name {{
            display: flex;
            align-items: center;
            gap: 10px;
            font-weight: 700;
        }}

        .expand-indicator {{
            display: inline-flex;
            width: 22px;
            height: 22px;
            align-items: center;
            justify-content: center;
            border-radius: 50%;
            background: #e8ecff;
            color: #4f46e5;
            font-size: 12px;
            transition: transform 0.2s ease;
        }}

        .assignee-row.open .expand-indicator {{
            transform: rotate(90deg);
        }}

        .assignee-tasks-row {{
            display: none;
            background: #f8faff;
        }}

        .assignee-tasks-row.open {{
            display: table-row;
        }}

        .assignee-tasks-wrap {{
            padding: 18px 20px 18px 48px;
        }}

        .assignee-detail-panel {{
            display: flex;
            flex-direction: column;
            gap: 18px;
        }}

        .assignee-detail-summary {{
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            align-items: center;
            color: #475467;
            font-size: 13px;
        }}

        .assignee-detail-hint {{
            color: #667085;
            font-size: 12px;
        }}

        .assignee-tag-filters {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
        }}

        .assignee-tag-filter {{
            border: 1px solid #d0d5dd;
            background: white;
            color: #344054;
            border-radius: 999px;
            padding: 8px 12px;
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s ease;
        }}

        .assignee-tag-filter:hover {{
            border-color: #98a2b3;
            background: #f8fafc;
        }}

        .assignee-tag-filter.active {{
            background: #4f46e5;
            border-color: #4f46e5;
            color: white;
            box-shadow: 0 8px 18px rgba(79, 70, 229, 0.18);
        }}

        .assignee-tag-filter-count {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-width: 20px;
            height: 20px;
            margin-left: 8px;
            padding: 0 6px;
            border-radius: 999px;
            background: rgba(15, 23, 42, 0.08);
            font-size: 11px;
        }}

        .assignee-tag-filter.active .assignee-tag-filter-count {{
            background: rgba(255, 255, 255, 0.18);
        }}

        .assignee-task-groups {{
            display: grid;
            gap: 14px;
        }}

        .assignee-task-group {{
            background: white;
            border: 1px solid #e4e7ec;
            border-radius: 14px;
            padding: 14px 16px;
            box-shadow: 0 8px 24px rgba(15, 23, 42, 0.04);
        }}

        .assignee-task-group-header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            margin-bottom: 12px;
        }}

        .assignee-task-group-title {{
            display: flex;
            align-items: center;
            gap: 10px;
            font-size: 15px;
            font-weight: 700;
            color: #101828;
        }}

        .assignee-task-group-count {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-width: 26px;
            height: 26px;
            padding: 0 8px;
            border-radius: 999px;
            background: #eef2ff;
            color: #4338ca;
            font-size: 12px;
            font-weight: 700;
        }}

        .assignee-task-list {{
            margin: 0;
            padding-left: 20px;
        }}

        .assignee-task-list li {{
            margin-bottom: 10px;
            line-height: 1.4;
        }}

        .assignee-task-meta {{
            color: #667085;
            font-size: 12px;
            margin-top: 2px;
        }}
        
        .badge {{
            display: inline-block;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 12px;
            font-weight: 500;
            margin: 2px;
            background: #e3f2fd;
            color: #1565c0;
        }}
        
        .badge-total {{
            background: #667eea;
            color: white;
            font-weight: 700;
        }}
        
        .badge-none {{
            background: #f5f5f5;
            color: #616161;
        }}
        
        .footer {{
            text-align: center;
            color: white;
            margin-top: 30px;
            padding: 20px;
        }}
        
        @media print {{
            body {{
                background: white;
            }}
            .stat-card, .chart-card, .table-card {{
                break-inside: avoid;
            }}
        }}
        
        @media (max-width: 768px) {{
            .charts-grid {{
                grid-template-columns: 1fr;
            }}
            table {{
                font-size: 12px;
            }}
            th, td {{
                padding: 6px;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📊 Отчёт по дежурным</h1>
            <div class="subtitle">Статистика закрытых задач</div>
            <div class="period">Период: {period['start']} — {period['end']} ({period['days']} дней)</div>
        </div>
        
        <div class="stats-grid">
            <div class="stat-card">
                <div class="number">{report_data['total_tasks']}</div>
                <div class="label">Всего закрыто задач</div>
            </div>
            <div class="stat-card">
                <div class="number">{stats['assignee_count']}</div>
                <div class="label">Сотрудников в отчёте</div>
            </div>
            <div class="stat-card">
                <div class="number">{stats['avg_per_assignee']}</div>
                <div class="label">Среднее на сотрудника</div>
            </div>
        </div>
        
        <div class="charts-grid">
            <div class="chart-card">
                <h3>📈 Закрытые задачи по сотрудникам</h3>
                <div class="chart-container">
                    <canvas id="assigneesChart"></canvas>
                </div>
            </div>
            <div class="chart-card">
                <h3>🎯 Распределение по тегам</h3>
                <div class="chart-container">
                    <canvas id="tagsChart"></canvas>
                </div>
            </div>
        </div>
        
        <div class="table-card">
            <h3>📋 Детальная статистика по сотрудникам</h3>
            <table>
                <thead>
                    <tr>
                        <th>Сотрудник</th>
                        <th>Всего</th>
                        {table_headers}
                    </tr>
                </thead>
                <tbody>
                    {table_rows}
                </tbody>
            </table>
        </div>

        <div class="footer">
            <p>Отчёт сгенерирован: {report_data['generated_at']}</p>
        </div>
    </div>
    
    <script>
        function toggleAssigneeTasks(assigneeKey) {{
            const summaryRow = document.getElementById(`assignee-row-${{assigneeKey}}`);
            const detailRow = document.getElementById(`assignee-tasks-${{assigneeKey}}`);
            if (!summaryRow || !detailRow) {{
                return;
            }}
            summaryRow.classList.toggle('open');
            detailRow.classList.toggle('open');
        }}

        function filterAssigneeTasks(assigneeKey, filterKey, event) {{
            if (event) {{
                event.stopPropagation();
            }}

            const detailPanel = document.getElementById(`assignee-detail-${{assigneeKey}}`);
            if (!detailPanel) {{
                return;
            }}

            detailPanel.querySelectorAll('.assignee-tag-filter').forEach((button) => {{
                button.classList.toggle('active', button.dataset.filter === filterKey);
            }});

            detailPanel.querySelectorAll('.assignee-task-group').forEach((group) => {{
                const shouldShow = filterKey === 'all' || group.dataset.tag === filterKey;
                group.style.display = shouldShow ? 'block' : 'none';
            }});
        }}

        // Столбчатая диаграмма по сотрудникам
        const assigneesCtx = document.getElementById('assigneesChart').getContext('2d');
        new Chart(assigneesCtx, {{
            type: 'bar',
            data: {{
                labels: {json.dumps(assignee_names, ensure_ascii=False)},
                datasets: [{{
                    label: 'Закрытые задачи',
                    data: {json.dumps(assignee_totals)},
                    backgroundColor: 'rgba(102, 126, 234, 0.8)',
                    borderColor: 'rgba(102, 126, 234, 1)',
                    borderWidth: 1,
                    borderRadius: 8
                }}]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                plugins: {{
                    legend: {{
                        display: false
                    }}
                }},
                scales: {{
                    y: {{
                        beginAtZero: true,
                        ticks: {{
                            stepSize: 1
                        }}
                    }},
                    x: {{
                        ticks: {{
                            autoSkip: false,
                            maxRotation: 45,
                            minRotation: 45
                        }}
                    }}
                }}
            }}
        }});
        
        // Круговая диаграмма по типам задач
        const tagsCtx = document.getElementById('tagsChart').getContext('2d');
        new Chart(tagsCtx, {{
            type: 'doughnut',
            data: {{
                labels: {json.dumps(tag_labels, ensure_ascii=False)},
                datasets: [{{
                    data: {json.dumps(tag_values)},
                    backgroundColor: {json.dumps(tag_colors)},
                    borderWidth: 2,
                    borderColor: '#fff'
                }}]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                plugins: {{
                    legend: {{
                        position: 'bottom',
                        labels: {{
                            padding: 15,
                            usePointStyle: true,
                            font: {{
                                size: 11
                            }}
                        }}
                    }}
                }}
            }}
        }});
    </script>
</body>
</html>'''
        
        return html
    
    def _generate_table_headers(self, all_tags: List[str]) -> str:
        """Генерирует заголовки таблицы на основе всех тегов"""
        headers = []
        
        # Сначала добавляем теги (кроме 'no_tags')
        for tag in sorted(all_tags):
            headers.append(f'<th>{tag.capitalize()}</th>')
        
        # Добавляем колонку для задач без тегов
        headers.append('<th>(без тега)</th>')
        
        return '\n                        '.join(headers)
    
    def _generate_table_rows(self, by_assignee: Dict, all_tags: List[str]) -> str:
        """Генерирует строки таблицы с динамическими тегами"""
        rows = []
        
        for index, (name, data) in enumerate(by_assignee.items(), 1):
            assignee_key = self._slugify_assignee(name, index)
            # Собираем ячейки для каждого тега
            tag_cells = []
            
            for tag in sorted(all_tags):
                count = data['tags'].get(tag, 0)
                if count > 0:
                    tag_cells.append(f'<td><span class="badge">{count}</span></td>')
                else:
                    tag_cells.append('<td>-</td>')
            
            # Добавляем ячейку для задач без тегов
            no_tags = data.get('no_tags', 0)
            if no_tags > 0:
                tag_cells.append(f'<td><span class="badge badge-none">{no_tags}</span></td>')
            else:
                tag_cells.append('<td>-</td>')
            
            details_html = self._generate_assignee_tasks_html(data.get('tasks', []), assignee_key)
            row = f'''
                <tr id="assignee-row-{assignee_key}" class="assignee-row" onclick="toggleAssigneeTasks('{assignee_key}')">
                    <td>
                        <div class="assignee-name">
                            <span class="expand-indicator">▶</span>
                            <span>{html.escape(name)}</span>
                        </div>
                    </td>
                    <td><span class="badge badge-total">{data['total']}</span></td>
                    {' '.join(tag_cells)}
                </tr>
                <tr id="assignee-tasks-{assignee_key}" class="assignee-tasks-row">
                    <td colspan="{len(all_tags) + 3}">
                        <div class="assignee-tasks-wrap">
                            {details_html}
                        </div>
                    </td>
                </tr>
            '''
            rows.append(row)
        
        return ''.join(rows)

    def _generate_assignee_tasks_html(self, tasks: List[Dict[str, Any]], assignee_key: str) -> str:
        """Генерирует структурированный блок задач сотрудника с фильтрацией по тегам."""
        if not tasks:
            return '<div>Нет закрытых задач за период</div>'

        grouped_tasks = self._group_tasks_by_first_label(tasks)
        total_tags = len(grouped_tasks)

        filter_buttons = [
            f'''
            <button class="assignee-tag-filter active" data-filter="all" onclick="filterAssigneeTasks('{assignee_key}', 'all', event)">
                Все теги
                <span class="assignee-tag-filter-count">{len(tasks)}</span>
            </button>
            '''
        ]

        group_blocks = []
        for group_index, (tag_name, grouped_items) in enumerate(grouped_tasks.items(), 1):
            tag_key = self._slugify_tag(tag_name, group_index)
            filter_buttons.append(
                f'''
                <button class="assignee-tag-filter" data-filter="{tag_key}" onclick="filterAssigneeTasks('{assignee_key}', '{tag_key}', event)">
                    {html.escape(tag_name)}
                    <span class="assignee-tag-filter-count">{len(grouped_items)}</span>
                </button>
                '''
            )

            items = []
            for index, task in enumerate(grouped_items, 1):
                closed_at = (task.get('resolutiondate') or task.get('updated') or '')[:16]
                labels = ', '.join(task.get('labels', [])) or '(без тега)'
                items.append(
                    f'''
                    <li>
                        <a href="{html.escape(task.get('url', '#'))}" target="_blank">{html.escape(task.get('key', ''))}</a>
                        {' - '}{html.escape(task.get('summary', ''))}
                        <div class="assignee-task-meta">#{index} | Закрыта: {html.escape(closed_at)} | Все теги: {html.escape(labels)}</div>
                    </li>
                    '''
                )

            group_blocks.append(
                f'''
                <section class="assignee-task-group" data-tag="{tag_key}">
                    <div class="assignee-task-group-header">
                        <div class="assignee-task-group-title">
                            <span>{html.escape(tag_name)}</span>
                            <span class="assignee-task-group-count">{len(grouped_items)}</span>
                        </div>
                    </div>
                    <ol class="assignee-task-list">{"".join(items)}</ol>
                </section>
                '''
            )

        return f'''
        <div class="assignee-detail-panel" id="assignee-detail-{assignee_key}">
            <div class="assignee-detail-summary">
                <span><strong>Задач за период:</strong> {len(tasks)}</span>
                <span><strong>Тегов в раскрытии:</strong> {total_tags}</span>
                <span class="assignee-detail-hint">Фильтр считает задачу по первому тегу, как и основная таблица.</span>
            </div>
            <div class="assignee-tag-filters">
                {"".join(filter_buttons)}
            </div>
            <div class="assignee-task-groups">
                {"".join(group_blocks)}
            </div>
        </div>
        '''

    def _group_tasks_by_first_label(self, tasks: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        """Группирует задачи по первому тегу с сортировкой по количеству и дате закрытия."""
        grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

        sorted_tasks = sorted(
            tasks,
            key=lambda task: task.get('resolutiondate') or task.get('updated') or '',
            reverse=True
        )

        for task in sorted_tasks:
            tag_name = task.get('first_label') or '(без тега)'
            grouped[tag_name].append(task)

        sorted_groups = sorted(
            grouped.items(),
            key=lambda item: (-len(item[1]), item[0].lower())
        )
        return dict(sorted_groups)

    def _slugify_assignee(self, name: str, index: int) -> str:
        """Генерирует безопасный идентификатор для HTML."""
        slug = re.sub(r'[^a-zA-Zа-яА-Я0-9_-]+', '-', name.strip().lower())
        slug = slug.strip('-') or 'assignee'
        return f'{index}-{slug}'

    def _slugify_tag(self, name: str, index: int) -> str:
        """Генерирует безопасный идентификатор для фильтра по тегу."""
        slug = re.sub(r'[^a-zA-Zа-яА-Я0-9_-]+', '-', name.strip().lower())
        slug = slug.strip('-') or 'tag'
        return f'{index}-{slug}'


# Singleton
_report_service = None


def get_report_service() -> ReportService:
    """Возвращает singleton экземпляр сервиса отчётов"""
    global _report_service
    if _report_service is None:
        _report_service = ReportService()
    return _report_service


def _ensure_reports_dir():
    """Создаёт папку reports/, если её нет"""
    if not os.path.exists(REPORTS_DIR):
        os.makedirs(REPORTS_DIR, exist_ok=True)
        logging.info(f"[REPORT] Создана папка для отчётов: {REPORTS_DIR}")


def save_report_to_disk(html_content: str) -> str:
    """
    Сохраняет HTML отчёт на диск и возвращает report_id.
    
    Args:
        html_content: HTML содержимое отчёта
        
    Returns:
        report_id: уникальный ID отчёта для скачивания
    """
    _ensure_reports_dir()
    
    # Генерируем уникальный ID
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    report_id = f"report_{timestamp}_{os.urandom(4).hex()}"
    
    # Сохраняем файл
    report_path = os.path.join(REPORTS_DIR, f"{report_id}.html")
    
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(html_content)
    
    logging.info(f"[REPORT] Отчёт сохранён: {report_path}")
    
    # Очищаем старые отчёты (старше 1 часа)
    cleanup_old_reports(max_age_hours=1)
    
    return report_id


def get_report_path(report_id: str) -> str:
    """
    Возвращает путь к файлу отчёта по ID.
    
    Args:
        report_id: ID отчёта
        
    Returns:
        Полный путь к файлу или None, если не найден
    """
    _ensure_reports_dir()
    
    report_path = os.path.join(REPORTS_DIR, f"{report_id}.html")
    
    if os.path.exists(report_path):
        return report_path
    
    return None


def cleanup_old_reports(max_age_hours: int = 1):
    """
    Удаляет отчёты старше указанного количества часов.
    
    Args:
        max_age_hours: максимальный возраст отчёта в часах
    """
    if not os.path.exists(REPORTS_DIR):
        return
    
    now = datetime.now()
    count = 0
    
    for filename in os.listdir(REPORTS_DIR):
        if not filename.endswith('.html'):
            continue
        
        filepath = os.path.join(REPORTS_DIR, filename)
        try:
            # Получаем время модификации файла
            mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
            age = (now - mtime).total_seconds() / 3600  # в часах
            
            if age > max_age_hours:
                os.remove(filepath)
                count += 1
                logging.info(f"[REPORT] Удалён старый отчёт: {filename}")
        except Exception as e:
            logging.warning(f"[REPORT] Ошибка при удалении {filename}: {e}")
    
    if count > 0:
        logging.info(f"[REPORT] Очищено {count} старых отчётов")
