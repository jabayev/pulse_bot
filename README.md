# pulse_bot

Telegram-бот мониторинга HTTP-сервисов. Периодически пингует заданные URL и уведомляет в чат при падении или восстановлении сервиса.

## Возможности

- Параллельная проверка любого количества HTTP-сервисов
- Уведомления только при изменении статуса (не спамит)
- Команды `/status`, `/status_camera` и `/list` прямо в Telegram
- Проверка RTSP-камер через TCP/RTSP `OPTIONS`
- Запуск в Docker одной командой

---

## Быстрый старт

### 1. Создать бота через @BotFather

1. Открой Telegram, найди **@BotFather**
2. Напиши `/newbot`
3. Придумай имя бота (например `My Pulse Bot`)
4. Придумай username — должен заканчиваться на `bot` (например `my_pulse_bot`)
5. BotFather пришлёт токен вида:
   ```
   123456789:AABBCCDDEEFFaabbccddeeff
   ```
   Сохрани его — это `TELEGRAM_BOT_TOKEN`

### 2. Получить Chat ID

**Для личных уведомлений:**
1. Найди своего бота по username в Telegram
2. Нажми **Start**
3. Открой в браузере:
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
4. В ответе найди `"chat": {"id": 123456789}` — это твой Chat ID

**Для группового чата:**
1. Создай группу и добавь в неё бота
2. Отключи Privacy Mode у бота: @BotFather → `/mybots` → выбери бота → **Bot Settings** → **Group Privacy** → **Turn off**
3. Удали бота из группы и добавь обратно
4. Напиши любое сообщение в группу
5. Открой `getUpdates` — найди `"chat": {"id": -1001234567890}` (отрицательное число)

### 3. Заполнить .env

```bash
cp .env.example .env
```

Открой `.env` и заполни:

```env
TELEGRAM_BOT_TOKEN=123456789:AABBCCDDEEFFaabbccddeeff
TELEGRAM_CHAT_ID=-1001234567890
CHECK_INTERVAL=60
SERVICES=[{"name":"My API","url":"https://api.example.com/health"},{"name":"Frontend","url":"https://example.com"}]
CAMERA_MONITOR_ENABLED=true
CAMERA_RTSP_URL_TEMPLATE=rtsp://username:password@example-camera-host:554/Streaming/Channels/{channel}
CAMERA_CHANNEL_START=101
CAMERA_CHANNEL_END=1501
CAMERA_CHANNEL_STEP=100
CAMERA_CHECK_TIMEOUT=5
```

| Переменная | Описание |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен от @BotFather |
| `TELEGRAM_CHAT_ID` | ID чата куда слать уведомления |
| `CHECK_INTERVAL` | Интервал проверки в секундах (по умолчанию 60) |
| `SERVICES` | JSON-массив сервисов, каждый с полями `name` и `url` |
| `CAMERA_MONITOR_ENABLED` | Включить проверку RTSP-камер (по умолчанию включается, если задан `CAMERA_RTSP_URL_TEMPLATE`) |
| `CAMERA_RTSP_URL_TEMPLATE` | Шаблон RTSP URL, где `{channel}` заменяется на номер канала |
| `CAMERA_CHANNEL_START` | Первый канал для проверки (по умолчанию `101`) |
| `CAMERA_CHANNEL_END` | Последний канал для проверки (по умолчанию `1501`) |
| `CAMERA_CHANNEL_STEP` | Шаг каналов (по умолчанию `100`) |
| `CAMERA_CHECK_TIMEOUT` | Таймаут ответа RTSP в секундах (по умолчанию `5`) |

### 4. Запустить через Docker

```bash
docker compose up -d
```

Проверить логи:
```bash
docker compose logs -f
```

Остановить:
```bash
docker compose down
```

---

## Команды бота

| Команда | Описание |
|---|---|
| `/status` | Текущий статус всех сервисов с временем последней проверки |
| `/status_camera` | Ручная проверка всех RTSP-камер |
| `/list` | Список отслеживаемых сервисов и интервал проверки |

---

## Структура проекта

```
├── main.py          # Точка входа, команды бота, планировщик задач
├── config.py        # Загрузка и валидация переменных окружения
├── monitor.py       # Асинхронная проверка HTTP-сервисов
├── camera_monitor.py # Асинхронная проверка RTSP-камер
├── notifier.py      # Отправка уведомлений в Telegram
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

---

## Пример уведомлений

**Сервис упал:**
```
🔴 Сервис недоступен

Сервис: My API
URL: https://api.example.com/health
Ошибка: Connection error: ...
Время: 2026-05-23 10:45:01 UTC
```

**Сервис восстановился:**
```
🟢 Сервис восстановлен

Сервис: My API
URL: https://api.example.com/health
Время ответа: 142.3 ms
Восстановлен: 2026-05-23 10:47:15 UTC
```
