# tg-alert-bot (pulse_bot)

Telegram-бот мониторинга BahandiML prod: приёмник алертов из Alertmanager,
сводки здоровья системы и метрики CV/VLM-продакшена по командам в группе.

## Что умеет

- **Приём алертов Alertmanager** через webhook (`POST /alertmanager`):
  critical — мгновенно с упоминанием дежурных, resolved — коротко,
  дедупликация по fingerprint, защита секретом (Bearer-токен)
- **Команды в группе**: `/status`, `/gpus`, `/containers`, `/cams`, `/frames`,
  `/algo`, `/face`, `/vlm`, `/webhooks`, `/alerts`, `/silence`, `/ack`, `/digest`
- **GPU-мониторинг** через DCGM-exporter (температура, VRAM, util, power,
  контроль количества карт)
- **Контейнеры prod-проекта** через Docker API: падение критичного контейнера
  или restart-loop → critical в группу, восстановление → resolved
- **Silence и ack** прямо из Telegram (Alertmanager API v2)
- **Дайджесты** по расписанию (кадры, камеры, алгоритмы, face, VLM, GPU, диск)
- **Роли**: админы (все команды) / участники группы (чтение); чужие чаты игнорируются
- Плюс прежний функционал: проверка HTTP-сервисов, RTSP-камер,
  метрики хоста с порогами и антидребезгом
- Часовой пояс сообщений: `Asia/Almaty`, язык — RU

## Архитектура

```
Prometheus ──► Alertmanager ──► POST http://tg-alert-bot:8080/alertmanager ──► TG-группа
     ▲                                        (webhook, secret)
     │
     └──── PromQL-запросы бота (команды /gpus /cams /frames ..., дайджесты)

Docker API (/var/run/docker.sock) ──► статус контейнеров + алерты о падениях
```

Логика правил живёт в Prometheus (`deploy/prometheus-alerts-bahandi.yml`);
бот — доставка, команды и небольшой собственный опрос (контейнеры, пороги хоста).

## Быстрый старт

### 1. Бот и группа

1. Создайте бота у **@BotFather** (`/newbot`), сохраните токен
2. Создайте рабочую группу, добавьте бота
3. Отключите Privacy Mode: @BotFather → `/mybots` → Bot Settings → Group Privacy → Turn off
4. Узнайте chat_id группы: напишите в группу и откройте
   `https://api.telegram.org/bot<TOKEN>/getUpdates` — `"chat":{"id":-100...}`
5. Узнайте свои user_id (например через @userinfobot) для `TELEGRAM_ADMIN_IDS`

### 2. Конфиг

```bash
cp .env.example .env
```

Минимум для запуска:

| Переменная | Описание |
|---|---|
| `TELEGRAM_BOT_TOKEN` | токен от @BotFather |
| `TELEGRAM_CHAT_ID` | id рабочей группы |
| `TELEGRAM_ADMIN_IDS` | user_id админов через запятую |
| `TELEGRAM_ADMIN_MENTIONS` | `@username` дежурных для critical |
| `PROMETHEUS_URL` | напр. `http://host.docker.internal:9090` |
| `ALERTMANAGER_URL` | напр. `http://host.docker.internal:9093` |
| `WEBHOOK_SECRET` | секрет приёмника (тот же в alertmanager.yml) |
| `COMPOSE_PROJECT_NAME` | `mlbahandi-prod` |

Полный список с комментариями — в [.env.example](.env.example).

### 3. Запуск

```bash
docker compose up -d --build
```

Бот поднимет webhook-приёмник на порту `8080` (`/alertmanager`, `/healthz`).

### 4. Подключение Alertmanager

1. Добавьте правила из [deploy/prometheus-alerts-bahandi.yml](deploy/prometheus-alerts-bahandi.yml)
   в `rule_files` Prometheus (поправьте имена job'ов под свой scrape-конфиг)
2. Настройте receiver по образцу [deploy/alertmanager-example.yml](deploy/alertmanager-example.yml):
   `monitoring/webhook.url` → `http://tg-alert-bot:8080/alertmanager`,
   Bearer-токен = `WEBHOOK_SECRET`
3. Перезагрузите конфиги: `curl -X POST http://localhost:9090/-/reload` и
   `curl -X POST http://localhost:9093/-/reload`

Проверка доставки (должно прийти в группу < 1 мин):

```bash
curl -X POST http://localhost:9093/api/v2/alerts -H 'Content-Type: application/json' -d '[{"labels":{"alertname":"TestAlert","severity":"critical"},"annotations":{"description":"проверка доставки"}}]'
```

## Команды

| Команда | Доступ | Описание |
|---|---|---|
| `/status` | все | сводка: контейнеры, GPU, Prometheus, Alertmanager, Ollama |
| `/alerts` | все | активные firing из Alertmanager |
| `/gpus` | admin | temp, power, util, VRAM, PCIe по каждой карте |
| `/containers` | admin | Up/Restarting/Exited по prod-проекту |
| `/cams` | admin | камеры ML online/offline, top reconnects |
| `/frames [1h\|24h]` | admin | кадры total + top камеры |
| `/algo [имя] [24h]` | admin | алерты по алгоритмам + инференсы моделей |
| `/face [1h\|24h]` | admin | confirmed / unknown / no_face / pending |
| `/vlm` | admin | Ollama, VLM-инференсы, circuit breaker |
| `/webhooks [1h]` | admin | success/retry/failed доставки событий |
| `/silence <alert> <30m\|2h\|1d>` | admin | silence через Alertmanager API |
| `/ack <alert>` | admin | пометить «взял в работу» |
| `/digest [12h\|24h]` | admin | ручной дайджест |
| `/metrics` | admin | CPU/RAM/диск/сеть хоста |
| `/thresholds` | admin | пороги встроенных алертов бота |
| `/status_camera`, `/refresh_cameras`, `/list` | admin | RTSP-камеры и обзор конфигурации |
| `/help` | все | список команд |

Роли: `TELEGRAM_ADMIN_IDS` — админы (могут писать боту и в ЛС); остальные
участники группы — только чтение (`/status`, `/alerts`, `/help`). Команды из
чужих чатов игнорируются.

## Формат алертов

```
🚨 CRITICAL | CameraStreamLikelyInactive
camera: store-12-entrance
for: 6м
desc: stream status=0
```

- critical → сразу + упоминание `TELEGRAM_ADMIN_MENTIONS`
- warning → сразу или батчем (`WARNING_BATCH_SECONDS` при шторме)
- resolved → `✅ RESOLVED | <имя> · длилось NN`
- повтор того же алерта не чаще `ALERT_DEDUP_SECONDS` (по fingerprint)
- поток алертов идёт через ограниченную очередь: падение Telegram не ломает
  Alertmanager (ретраи, при переполнении — drop с логом)

## Дайджест

`DIGEST_TIMES=09:00,21:00` (время `TZ_NAME`). Содержимое: контейнеры, кадры,
камеры online, reconnects, алерты алгоритмов, face confirmed/unknown,
VLM-инференсы, GPU max temp / avg util / max VRAM, свободные диск и RAM,
top-5 проблемных камер. Вручную — `/digest 24h`.

## Источники данных

| Источник | Использование |
|---|---|
| Alertmanager webhook | основной поток алертов (firing/resolved) |
| Alertmanager API v2 | `/alerts`, `/silence` |
| Prometheus API | `/status`, `/gpus`, `/cams`, `/frames`, `/algo`, `/face`, `/vlm`, `/webhooks`, дайджесты |
| Docker API (socket) | `/containers`, алерты о падении контейнеров |
| node_exporter / psutil | `/metrics`, пороги хоста |

Метрики приложения: `ml_frames_processed_total`, `ml_stream_status`,
`ml_stream_reconnects_total`, `ml_events_delivered_total`,
`ml_algorithm_alerts_total`, `ml_model_inferences_total`,
`ml_face_results_total` и т.д. Имена label'ов камер бот определяет гибко
(`camera` / `cam` / `location` / `name` / ...).

## Проверка по критериям приёмки

1. Тестовый firing (curl выше) приходит в группу < 60 с; resolved тоже
2. `docker stop <analytics-worker>` → через ~1.5 мин critical `ContainerDown`,
   `docker start` → `RESOLVED`
3. Симуляция GPU-алертов — правила `GpuTempHigh/GpuTempCritical/GpuMissing`
   в deploy-файле; отвал экспортёра глушит температурные (inhibit)
4. `/status` показывает обе GPU и ключевые контейнеры
5. `/frames 1h` сверяется с `sum(increase(ml_frames_processed_total[1h]))`
6. Команда из чужого чата — игнор (см. лог `Ignoring command`)
7. При недоступном Telegram алерты копятся в ограниченной очереди и
   ретраятся; Alertmanager всегда получает быстрый 200

## Структура проекта

```
├── main.py           # Точка входа: команды, джобы, планировщик
├── config.py         # Загрузка и валидация переменных окружения
├── access.py         # Роли и whitelist чатов
├── am_webhook.py     # Приёмник Alertmanager → Telegram (aiohttp)
├── am_client.py      # Alertmanager API: активные алерты, silences
├── prom.py           # Клиент Prometheus API
├── gpu.py            # GPU через DCGM-exporter
├── ml_reports.py     # Отчёты /cams /frames /algo /face /vlm /webhooks
├── containers.py     # Docker API: статус и watcher контейнеров
├── digest.py         # Периодический дайджест
├── monitor.py        # Проверка HTTP-сервисов
├── camera_monitor.py # Проверка RTSP-камер
├── camera_source.py  # Динамический список камер
├── metrics.py        # Метрики хоста (Prometheus / psutil)
├── alerts.py         # Встроенные пороги хоста
├── notifier.py       # Отправка в Telegram (ретраи, mentions, threads)
├── deploy/
│   ├── prometheus-alerts-bahandi.yml   # Alert rules (раздел 6.3 ТЗ)
│   └── alertmanager-example.yml        # Пример route/receiver/inhibit
├── docker-compose.yml
└── .env.example
```
