/**
 * Чат-бот для дашборда дежурного
 * Frontend логика чата
 */

class DashboardChatBot {
    constructor() {
        this.isOpen = false;
        this.isEmbedded = Boolean(document.getElementById('embeddedChatBot'));
        this.messages = [];
        this.sessionId = null;
        this.isLoading = false;
        this.basePath = window.CHATBOT_BASE_PATH || '';
        
        // DOM элементы
        this.chatWidget = null;
        this.chatToggle = null;
        this.chatContainer = null;
        this.messagesContainer = null;
        this.inputField = null;
        this.sendButton = null;
        this.suggestionsContainer = null;
        
        this.init();
    }

    buildUrl(path) {
        if (!path) {
            return this.basePath || '';
        }

        if (/^https?:\/\//i.test(path)) {
            return path;
        }

        if (this.basePath && path.startsWith(`${this.basePath}/`)) {
            return path;
        }

        if (path.startsWith('/')) {
            return `${this.basePath}${path}`;
        }

        return `${this.basePath}/${path}`;
    }
    
    init() {
        // Создаём DOM структуру
        this.createDOM();
        
        // Привязываем события
        this.bindEvents();
        
        // Загружаем историю
        this.loadHistory();
        
        console.log('ChatBot initialized');
    }
    
    createDOM() {
        const embeddedHost = document.getElementById('embeddedChatBot');
        if (embeddedHost) {
            this.chatWidget = document.createElement('div');
            this.chatWidget.className = 'chat-widget embedded open';
            this.chatWidget.innerHTML = `
            <div class="chat-header">
                <div class="chat-title">
                    <i class="bi bi-robot"></i>
                    <div class="chat-title-block">
                        <span class="chat-title-label">AI-агент OPLOT</span>
                        <span class="chat-title-subtitle">Релизы, документы, Confluence, смена</span>
                    </div>
                </div>
                <div class="chat-actions">
                    <button class="chat-action-btn" id="chat-clear" title="Очистить историю">
                        <i class="bi bi-trash"></i>
                    </button>
                </div>
            </div>
            <div class="chat-messages" id="chat-messages"></div>
            <div class="chat-suggestions" id="chat-suggestions"></div>
            <div class="chat-input-container">
                <button class="chat-reset-btn" id="chat-clear-inline" title="Сбросить диалог">
                    <i class="bi bi-arrow-counterclockwise"></i>
                    <span>Сброс</span>
                </button>
                <textarea 
                    class="chat-input" 
                    id="chat-input" 
                    rows="1"
                    placeholder="Например: какие релизы текущей недели закреплены за Ивановым?"
                ></textarea>
                <button class="chat-send-btn" id="chat-send">
                    <i class="bi bi-send-fill"></i>
                </button>
            </div>
            `;
            embeddedHost.appendChild(this.chatWidget);
            this.messagesContainer = this.chatWidget.querySelector('#chat-messages');
            this.suggestionsContainer = this.chatWidget.querySelector('#chat-suggestions');
            this.inputField = this.chatWidget.querySelector('#chat-input');
            this.sendButton = this.chatWidget.querySelector('#chat-send');
            const embeddedTitle = this.chatWidget.querySelector('.chat-title-label');
            if (embeddedTitle) {
                embeddedTitle.textContent = 'AI-бот Oplot';
            }
            this.inputField.placeholder = 'Например: какие релизы текущей недели закреплены за Ивановым?';
            this.isOpen = true;
            return;
        }
        // Создаём кнопку открытия чата
        this.chatToggle = document.createElement('div');
        this.chatToggle.className = 'chat-toggle';
        this.chatToggle.innerHTML = `
            <i class="bi bi-chat-dots-fill"></i>
            <span class="chat-toggle-badge" style="display: none;">0</span>
        `;
        document.body.appendChild(this.chatToggle);
        
        // Создаём контейнер чата
        this.chatWidget = document.createElement('div');
        this.chatWidget.className = 'chat-widget';
        this.chatWidget.innerHTML = `
            <div class="chat-header">
                <div class="chat-title">
                    <i class="bi bi-robot"></i>
                    <div class="chat-title-block">
                        <span class="chat-title-label">AI-бот Oplot</span>
                        <span class="chat-title-subtitle">Релизы, документы, Confluence, смена</span>
                    </div>
                </div>
                <div class="chat-actions">
                    <button class="chat-action-btn" id="chat-expand" title="Увеличить окно">
                        <i class="bi bi-fullscreen"></i>
                    </button>
                    <button class="chat-action-btn" id="chat-clear" title="Очистить историю">
                        <i class="bi bi-trash"></i>
                    </button>
                    <button class="chat-action-btn" id="chat-close" title="Закрыть">
                        <i class="bi bi-x-lg"></i>
                    </button>
                </div>
            </div>
            <div class="chat-messages" id="chat-messages"></div>
            <div class="chat-suggestions" id="chat-suggestions"></div>
            <div class="chat-input-container">
                <textarea 
                    class="chat-input" 
                    id="chat-input" 
                    rows="1"
                ></textarea>
                <button class="chat-send-btn" id="chat-send">
                    <i class="bi bi-send-fill"></i>
                </button>
            </div>
        `;
        document.body.appendChild(this.chatWidget);
        
        // Получаем ссылки на элементы
        this.messagesContainer = this.chatWidget.querySelector('#chat-messages');
        this.suggestionsContainer = this.chatWidget.querySelector('#chat-suggestions');
        this.inputField = this.chatWidget.querySelector('#chat-input');
        this.sendButton = this.chatWidget.querySelector('#chat-send');
    }
    
    bindEvents() {
        if (this.isEmbedded) {
            this.sendButton.addEventListener('click', () => this.sendMessage());

            this.inputField.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    this.sendMessage();
                }
            });

            this.inputField.addEventListener('input', () => {
                this.inputField.style.height = 'auto';
                this.inputField.style.height = Math.min(this.inputField.scrollHeight, 120) + 'px';
            });

            this.chatWidget.querySelector('#chat-clear').addEventListener('click', () => this.clearHistory());
            const inlineClearBtn = this.chatWidget.querySelector('#chat-clear-inline');
            if (inlineClearBtn) {
                inlineClearBtn.addEventListener('click', () => this.clearHistory());
            }
            return;
        }
        // Открытие/закрытие чата
        this.chatToggle.addEventListener('click', () => this.toggle());
        
        this.chatWidget.querySelector('#chat-close').addEventListener('click', () => this.close());
        
        // Отправка сообщения
        this.sendButton.addEventListener('click', () => this.sendMessage());
        
        this.inputField.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                this.sendMessage();
            }
        });
        
        // Автоматическое изменение высоты textarea
        this.inputField.addEventListener('input', () => {
            this.inputField.style.height = 'auto';
            this.inputField.style.height = Math.min(this.inputField.scrollHeight, 120) + 'px';
        });
        
        // Очистка истории
        this.chatWidget.querySelector('#chat-clear').addEventListener('click', () => this.clearHistory());
        
        // Увеличение/уменьшение окна
        this.chatWidget.querySelector('#chat-expand').addEventListener('click', () => this.toggleExpand());
    }
    
    toggleExpand() {
        const isExpanded = this.chatWidget.classList.toggle('expanded');
        const expandBtn = this.chatWidget.querySelector('#chat-expand');
        const icon = expandBtn.querySelector('i');
        
        if (isExpanded) {
            icon.classList.remove('bi-fullscreen');
            icon.classList.add('bi-fullscreen-exit');
            expandBtn.title = 'Уменьшить окно';
        } else {
            icon.classList.remove('bi-fullscreen-exit');
            icon.classList.add('bi-fullscreen');
            expandBtn.title = 'Увеличить окно';
        }
        
        // Прокручиваем к последнему сообщению
        this.scrollToBottom();
    }
    
    toggle() {
        this.isOpen = !this.isOpen;
        this.chatWidget.classList.toggle('open', this.isOpen);
        this.chatToggle.classList.toggle('active', this.isOpen);
        
        if (this.isOpen) {
            this.inputField.focus();
            this.scrollToBottom();
        }
    }
    
    open() {
        this.isOpen = true;
        this.chatWidget.classList.add('open');
        this.chatToggle.classList.add('active');
        this.inputField.focus();
        this.scrollToBottom();
    }
    
    close() {
        this.isOpen = false;
        this.chatWidget.classList.remove('open');
        this.chatToggle.classList.remove('active');
        
        // Сбрасываем размер окна при закрытии
        if (this.chatWidget.classList.contains('expanded')) {
            this.chatWidget.classList.remove('expanded');
            const expandBtn = this.chatWidget.querySelector('#chat-expand');
            const icon = expandBtn.querySelector('i');
            icon.classList.remove('bi-fullscreen-exit');
            icon.classList.add('bi-fullscreen');
            expandBtn.title = 'Увеличить окно';
        }
    }
    
    async sendMessage() {
        const message = this.inputField.value.trim();
        if (!message || this.isLoading) return;
        
        // Добавляем сообщение пользователя
        this.addMessage('user', message);
        
        // Очищаем поле ввода
        this.inputField.value = '';
        this.inputField.style.height = 'auto';
        
        // Показываем индикатор загрузки
        this.setLoading(true);
        
        try {
            // Получаем контекст дашборда
            const dashboardContext = this.getDashboardContext();
            
            // Отправляем запрос
            const response = await fetch(this.buildUrl('/dashboard/api/chat'), {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    message: message,
                    context: dashboardContext
                })
            });
            
            const data = await response.json();
            
            if (data.success) {
                // Добавляем ответ бота
                this.addMessage('assistant', data.response.text, {
                    intent: data.response.intent,
                    metadata: data.response.metadata
                });
                
                // Обновляем подсказки
                this.updateSuggestions(data.response.suggestions);
            } else {
                this.addMessage('assistant', '❌ Произошла ошибка: ' + (data.error || 'Неизвестная ошибка'));
            }
        } catch (error) {
            console.error('Chat error:', error);
            this.addMessage('assistant', '❌ Ошибка соединения. Попробуйте позже.');
        } finally {
            this.setLoading(false);
        }
    }
    
    addMessage(role, content, metadata = {}) {
        const messageEl = document.createElement('div');
        messageEl.className = `chat-message ${role}`;
        
        const time = new Date().toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
        
        // Обрабатываем markdown-style форматирование
        let formattedContent = this.formatMessage(content);
        
        messageEl.innerHTML = `
            <div class="chat-message-content">
                ${formattedContent}
            </div>
            <div class="chat-message-time">${time}</div>
        `;
        
        this.messagesContainer.appendChild(messageEl);
        this.scrollToBottom();
        
        // Сохраняем в историю
        this.messages.push({ role, content, metadata, time });
    }
    
    formatMessage(content) {
        // Экранируем HTML сначала
        let formatted = content
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
        
        // Обрабатываем markdown ссылки [текст](url) - ДО других преобразований
        // Используем специальную обработку для ссылок на скачивание отчётов
        formatted = formatted.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (match, text, url) => {
            // Декодируем HTML-сущности в URL для проверки
            const decodedUrl = url.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>');
            
            // Специальная обработка для ссылок на скачивание отчётов
            if (
                decodedUrl.includes('/dashboard/api/chat/report/download') ||
                decodedUrl.includes('/dashboard/api/chat/rov-statistics/download')
            ) {
                const reportUrl = this.buildUrl(decodedUrl);
                return `<a href="${reportUrl}" target="_blank" class="report-download-link" data-url="${reportUrl}">📥 ${text}</a>`;
            }
            return `<a href="${this.buildUrl(decodedUrl)}" target="_blank">${text}</a>`;
        });
        
        // Обрабатываем markdown жирный текст
        formatted = formatted.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
        formatted = formatted.replace(/\*(.*?)\*/g, '<strong>$1</strong>');
        
        // Код
        formatted = formatted.replace(/`([^`]+)`/g, '<code>$1</code>');

        // Раскрывающиеся блоки для длинных списков
        formatted = formatted.replace(/\[details=(.*?)\]([\s\S]*?)\[\/details\]/g, (match, title, body) => {
            return (
                `<details class="chat-details">` +
                `<summary>${title.trim()}</summary>` +
                `<div class="chat-details-body">${body.trim()}</div>` +
                `</details>`
            );
        });
        
        // Переносы строк
        formatted = formatted.replace(/\n/g, '<br>');
        
        // Emoji обработка
        const emojiMap = {
            '🔴': '🔴',
            '🟡': '🟡',
            '🟢': '🟢',
            '📊': '📊',
            '📋': '📋',
            '👤': '👤',
            '📅': '📅',
            '⚡': '⚡',
            '🏷️': '🏷️',
            '🔗': '🔗',
            '✅': '✅',
            '🔄': '🔄',
            '⚠️': '⚠️',
            '💡': '💡',
            '🔍': '🔍',
            '📖': '📖',
            '🤖': '🤖',
            '👋': '👋',
            '❌': '❌',
            '📂': '📂',
            '📥': '📥'
        };
        
        for (const [emoji, replacement] of Object.entries(emojiMap)) {
            formatted = formatted.split(emoji).join(`<span class="emoji">${replacement}</span>`);
        }
        
        return formatted;
    }
    
    updateSuggestions(suggestions) {
        if (!suggestions || suggestions.length === 0) {
            this.suggestionsContainer.innerHTML = '';
            return;
        }
        
        this.suggestionsContainer.innerHTML = suggestions.map(suggestion => `
            <button class="chat-suggestion-btn" data-text="${suggestion}">
                <span>${suggestion}</span>
            </button>
        `).join('');
        
        // Привязываем события к подсказкам
        this.suggestionsContainer.querySelectorAll('.chat-suggestion-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                this.inputField.value = btn.dataset.text;
                this.sendMessage();
            });
        });
    }
    
    async loadSuggestions(intent = null) {
        try {
            const url = intent 
                ? this.buildUrl(`/dashboard/api/chat/suggestions?intent=${encodeURIComponent(intent)}`)
                : this.buildUrl('/dashboard/api/chat/suggestions');
            
            const response = await fetch(url);
            const data = await response.json();
            
            if (data.success) {
                // Преобразуем в простой массив строк
                const suggestions = data.suggestions.map(s => s.text);
                this.updateSuggestions(suggestions);
            }
        } catch (error) {
            console.error('Error loading suggestions:', error);
        }
    }
    
    async loadHistory() {
        try {
            const response = await fetch(this.buildUrl('/dashboard/api/chat/history?limit=20'));
            const data = await response.json();
            
            if (data.success && data.history.length > 0) {
                // Очищаем текущие сообщения
                this.messagesContainer.innerHTML = '';
                this.messages = [];
                
                // Загружаем историю
                data.history.forEach(msg => {
                    this.addMessage(msg.role, msg.content, {
                        intent: msg.intent
                    });
                });
                
                // Загружаем подсказки для последнего intent
                const lastMessage = data.history[data.history.length - 1];
                if (lastMessage && lastMessage.intent) {
                    this.loadSuggestions(lastMessage.intent);
                }
            } else {
                // Приветственное сообщение
                this.addMessage('assistant', this.getWelcomeMessage());
                
                // Загружаем начальные подсказки
                this.loadSuggestions();
            }
        } catch (error) {
            console.error('Error loading history:', error);
        }
    }
    
    async clearHistory() {
        try {
            const response = await fetch(this.buildUrl('/dashboard/api/chat/clear'), {
                method: 'POST'
            });
            
            const data = await response.json();
            
            if (data.success) {
                this.messagesContainer.innerHTML = '';
                this.messages = [];
                
                // Приветственное сообщение
                this.addMessage('assistant', this.getWelcomeMessage());
                this.loadSuggestions();
            }
        } catch (error) {
            console.error('Error clearing history:', error);
        }
    }

    getWelcomeMessage() {
        if (this.isEmbedded) {
            return (
                '*Привет! Я Oplot.*\n\n' +
                'Я AI-бот команды OPLOT и готов помочь с твоими рабочими задачами: найти релизы, сформировать документы, обновить Confluence или собрать сводку. Напиши запрос обычными словами, а я подхвачу.'
            );
            return (
                '*AI-агент OPLOT*\n\n' +
                'Помогаю с релизами, документами, Confluence и сменными сводками.\n\n' +
                'Могу:\n' +
                '• показать релизы текущей недели по ответственному\n' +
                '• запустить сценарий формирования релизных документов\n' +
                '• найти инструкцию и Jenkins job для раскатки на ПСИ\n' +
                '• выгрузить таблицу релизов в Confluence\n' +
                '• найти задачи и подготовить сводку смены'
            );
        }
        return (
            '*AI-бот Oplot*\n\n' +
            'Помогаю с релизами, документами, Confluence и рабочим столом дежурного.\n\n' +
            'Могу:\n' +
            '• показать релизы недели по ответственному\n' +
            '• сформировать документы по релизу\n' +
            '• найти инструкцию и Jenkins job для раскатки на ПСИ\n' +
            '• выгрузить таблицу релизов в Confluence\n' +
            '• открыть Центр назначений и предложить ответственных\n' +
            '• найти задачи и подготовить сводку дневной или вечерней смены'
        );
    }
    
    setLoading(loading) {
        this.isLoading = loading;
        this.sendButton.disabled = loading;
        this.sendButton.innerHTML = loading 
            ? '<span class="spinner-border spinner-border-sm"></span>'
            : '<i class="bi bi-send-fill"></i>';
    }
    
    scrollToBottom() {
        this.messagesContainer.scrollTop = this.messagesContainer.scrollHeight;
    }
    
    getDashboardContext() {
        // Собираем контекст из страницы дашборда
        const context = {
            page_context: window.dashboardData?.page_context || 'dashboard',
            sup_tasks: window.dashboardData?.sup_tasks || [],
            logi_tasks: window.dashboardData?.logi_tasks || [],
            vnedrenie_prom_tasks: window.dashboardData?.vnedrenie_prom_tasks || [],
            vnedrenie_psi_tasks: window.dashboardData?.vnedrenie_psi_tasks || [],
            release_monitor: window.dashboardData?.release_monitor || [],
            release_monitor_summary: window.dashboardData?.release_monitor_summary || {},
            release_monitor_meta: window.dashboardData?.release_monitor_meta || {},
            assignee_stats: window.dashboardData?.assignee_stats || {}
        };
        
        return context;
    }
}

// Инициализация при загрузке страницы
document.addEventListener('DOMContentLoaded', () => {
    if (window.CHATBOT_DISABLED_BY_MAINTENANCE) {
        return;
    }
    window.dashboardChatBot = new DashboardChatBot();
});
