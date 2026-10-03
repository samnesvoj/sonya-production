# SONYA Project Context

Before making architecture decisions, read:
- docs/SONYA_AUDIT.md

This document contains the production audit findings and known risks.

Priority order:
1. P0 production blockers
2. P1 reliability/security issues
3. P2 cleanup and refactoring

Do not re-audit already fixed issues without checking git history first.мб это ему в мд засунуть SONYA — Технический аудит production-репозитория

Аудит выполнен только чтением кода (backend полностью прочитан вручную, frontend и GPU/deploy — через два независимых read-only агента). Код не менялся.

---
1. Архитектура

Frontend — статический vanilla JS, без сборки (app.js, auth.js, config.js, index.html, opencut.html, sphere.js). config.js задаёт SONYA_API_BASE = '/api' — same-origin, через reverse-proxy, без CORS-сложностей в проде.

Backend — FastAPI (scripts/prod_generation_api.py) + PostgreSQL (scripts/prod_job_store.py, auth_store.py). Два независимых контура авторизации:
- Браузер → HttpOnly/Secure/SameSite=Lax cookie sonya_session (passwordless email-code).
- Worker → Authorization: Bearer WORKER_SECRET (HMAC constant-time).

Worker / GPU pipeline — эфемерные GPU-инстансы (vast.ai — «предпочтительный провайдер»), поднимаются по требованию:
job создан → queued в Postgres → gpu_dispatcher.py (systemd, poll каждые 20с) → gpu_orchestrator.py создаёт инстанс на vast.ai → инстанс тянет ghcr.io/.../sonya-worker:fast, запускает worker_entrypoint.sh → gpu_worker.py --once клеймит job по HTTP → скачивает input и модели из S3 → выполняет mode → грузит результат в S3 → отчитывается /complete//fail.

Storage — S3-совместимое (Timeweb Cloud), presigned GET URL для результатов, чёткие key-паттерны по user_id/job_id/mode.

Deploy — Docker-образы собираются и пушатся в GHCR через GitHub Actions (только build, без тестов/линта); dispatcher — systemd-сервис на VPS.

---
2. Production readiness

Что готово хорошо:
- Auth-модуль (codes/sessions/cookies) спроектирован грамотно: HMAC-хэширование, единичное использование кода, rate-limit, origin-check, safe error responses.
- Upload-валидация (magic bytes + расширение + размер) сделана правильно.
- Cold-start SLA/blacklist логика для vast.ai (vast_bad_hosts.py, timeout/retry) реализована полно и работает по замыслу — сама по себе.

Что сломается при нагрузке / уже сломано:
- Баг клейма job для vast.ai (см. P0-1) — по факту делает GPU-пайплайн через vast.ai нерабочим прямо сейчас: диспетчер переводит job в gpu_requested до того, как воркер вообще стартовал, а claim_specific_job требует status='queued'. mark_worker_started() нигде не вызывается. Результат — job виснет до 240с (VAST_STARTUP_TIMEOUT_SEC) и уходит в retry/fail по SLA-таймауту, даже если воркер реально успел бы отработать.
- Дыра в concurrency cap: count_active_gpu_jobs() считает только ранние статусы (gpu_requested/gpu_booting/worker_started/model_downloading) и перестаёт учитывать job, как только он входит в mode_running — то есть в основную (самую длительную) фазу GPU-инференса. При MAX_ACTIVE_GPU_JOBS=1 система реально может поднять 2-й, 3-й инстанс, пока первый ещё считает — cap не защищает от параллельных расходов на GPU.
- Нет уничтожения инстанса при успехе job. destroy_vast_instance() вызывается только из ветки timeout/failure. При успешном завершении job инстанс явно не удаляется — риск "оплаченных, но простаивающих" инстансов на vast.ai.
- Нет CI-гейта: только сборка Docker-образов на push в main, без тестов, линта, validate_repo_integrity.py или smoke-теста образа.
- Схема БД: generation_jobs.user_id — TEXT, users.id — UUID (несовпадение типов, FK физически невозможен без миграции); два параллельных, местами рассинхронизированных механизма приоритета (queue_priority vs priority) и retry (retry_count vs attempts).

---
3. Security

Сильные стороны:
- Session/auth-коды — HMAC-SHA256 с серверным секретом, ничего "сырого" в БД не хранится.
- verify_worker_secret — constant-time compare, generic 403.
- security_audit.py фильтрует ключи с секретами перед записью, маскирует IP.
- CORS настроен строго: явные origin'ы, allow_credentials=True несовместим с * — код падает с RuntimeError при попытке так сконфигурировать прод.
- Секреты для vast.ai передаются в env-словаре HTTPS-запроса (не в URL/query), не логируются нигде (проверено по коду и validate_repo_integrity.py).

Проблемы:
- Git-clone fallback путь bootstrap (gpu_orchestrator.py:844-893, non-production dev-путь) клонирует origin/main без pinning на коммит/чексумму — supply-chain риск, если этот путь когда-либо активируется.
- .dockerignore лежит не в корне build-контекста (deploy/docker/.dockerignore, но context = .) — Docker его не применяет. Сейчас утечки нет только потому, что COPY явно ограничен scripts/, modes/, configs/ — но это хрупкая защита, а не то, что описано в комментариях Dockerfile.
- Frontend XSS: app.js:1172-1177 вставляет url из ответа API (result.url/result_url/…) в innerHTML без экранирования — при компрометации бэкенда/воркера возможна инъекция разметки.
- Legal-документы содержат видимые TODO для юрлица/реквизитов/контактов прямо в проде (legal/*.html) — не техническая уязвимость, но юридический/репутационный риск при запуске.

---
4. Frontend (после cleanup)

- Auth-cookie реализована корректно: credentials: 'include' везде, токен нигде не лежит в localStorage.
- Критичный баг интеграции: refreshAuthState() (единственный источник состояния "залогинен") стучится в GET /auth/session-status — эндпоинта с таким путём в описанном backend API нет (есть только /api/auth/me, который используется в другом месте и работает). Итог — после успешного логина UI, скорее всего, не покажет пользователя залогиненным.
- Отсутствует файл hf_20260419_..._(1).mp4 (тёмная тема / opencut.html) — на диске есть только bg-light.mp4, тёмная тема будет давать 404.
- opencut.html не интегрирован: открывается без передачи job/результата, редактор не получает видео пользователя.
- Оплата (payment/success.html, payment/fail.html) — статичные заглушки, никак не привязаны к реальному платёжному шлюзу; единственный рабочий "апгрейд" — переход в Telegram-бот, явно помечен как stub в коде.
- Legacy-код Telegram Mini App (sendDataToBot, хардкод admin ID) сосуществует с новым веб-флоу — двойные пути отправки формы.
- Poll job-статуса не отличает 401 (истёкшая сессия) от временной ошибки — пользователь может поллить до часа вникуда.
- Репо-bloat: bg-light.mp4 (15 МБ) в git, space-bg.png (96 КБ) закоммичен, но нигде не используется.

---
5. Итоговый список

P0 — критично (блокирует прод / основную функциональность)

1. GPU claim-баг: job, отправленный на vast.ai, никогда не может быть заклеймлен воркером (prod_job_store.py:331-354 требует status='queued', но mark_gpu_requested уже перевёл его в gpu_requested, а mark_worker_started() нигде не вызывается) — GPU-пайплайн через основного прод-провайдера сейчас нерабочий.
2. Frontend auth-баг: refreshAuthState() вызывает несуществующий /api/auth/session-status вместо рабочего /api/auth/me — состояние "залогинен" не обновляется после логина.
3. Дыра в concurrency cap: count_active_gpu_jobs() не считает mode_running и соседние статусы — MAX_ACTIVE_GPU_JOBS не защищает от параллельного расхода GPU в самой дорогой фазе job.
4. Нет self-destroy инстанса при успехе — риск накопления оплаченных, но не уничтоженных vast.ai-инстансов.
5. Legal-страницы с видимыми TODO (реквизиты, контакты) — нельзя запускать оплату/регистрацию с такими юр. документами.

P1 — важно

1. XSS-вектор в app.js:1172-1177 (unescaped innerHTML с URL из API-ответа).
2. Нет CI-гейта (тесты/линт/validate_repo_integrity.py) — все проверки только вручную по чеклисту.
3. Схема БД: user_id TEXT vs users.id UUID (FK невозможен), дублирующиеся системы приоритета/retry, которые могут рассинхронизироваться.
4. .dockerignore физически не работает (не в корне build-контекста) — единственная защита от утечки в образ держится на узком COPY.
5. Git-clone fallback bootstrap без pinning на коммит — supply-chain риск (non-prod путь, но существует).
6. Отсутствует файл фонового видео для тёмной темы/редактора — 404 у части пользователей.
7. Оплата — полная заглушка, нет реальной интеграции с платёжным провайдером, несмотря на готовую серверную billing-модель (plan_type, plan_status, free_video_limit).
8. opencut.html не связан с реальным результатом job.
9. Двойной legacy-код Telegram-бота параллельно с новым веб-auth — риск двойной отправки/путаницы при поддержке.
10. Поллинг статуса job не различает истёкшую сессию (401) от временной ошибки — пользователь может «зависнуть» без сообщения о повторном логине.

P2 — улучшения

1. Удалить/вынести в CDN bg-light.mp4 (15 МБ) и неиспользуемый space-bg.png (96 КБ) из git.
2. Убрать мёртвый код: apiGetSubscriptionStatus (не используется), нерабочая ветка "resume polling after reload" (недостижима в SPA).
3. Свести дублирующуюся логику API_BASE в app.js/auth.js к одному общему клиенту.
4. UI всё ещё собирает video URL, хотя backend гарантированно его отклоняет (только file-upload) — убрать несоответствующее поле или явно скрыть.
5. elements.voiceoverToggle.checked без guard (app.js:603) — потенциальный throw, если элемент отсутствует.
6. Убрать один из дублирующихся индексов (idx_jobs_priority_queue vs ix_jobs_dispatch_priority).

✻ Sautéed for 6m 5s

---

## Update: Robokassa payment integration (this change)

- **P1-7 (оплата — полная заглушка) — исправлено.** Реальная интеграция с Robokassa
  (checkout → redirect → ResultURL webhook с проверкой подписи → активация
  подписки) в `scripts/robokassa.py`, `scripts/payment_store.py`,
  `scripts/payment_routes.py`, миграция `010_payments.sql`. Полностью
  архивный поиск по git history подтвердил: до этого изменения в проекте не
  было НИ ОДНОЙ строки, связанной с Robokassa, ни в текущем дереве, ни в
  истории, ни в dangling-объектах/стэшах.
- Новый, ранее не задокументированный здесь баг найден и исправлен
  попутно: backend нигде не возвращал `402 FREE_PLAN_USED` при создании
  job (grep по всем `.py` — 0 совпадений), хотя `auth.js::checkAndCreateVideoJob`
  уже ждал этот код. Free-план был фактически безлимитным. См.
  `scripts/prod_generation_api.py::_is_pro_active` + gate перед созданием job
  в обоих job-creation путях (upload и URL-ingest).
- P2-2 (`apiGetSubscriptionStatus` не используется) — по-прежнему не
  используется, не трогали (вне периметра этой задачи).
- Recurring (автопродление) НЕ реализован — продуктовая модель
  (помесячные тарифы Start/Pro/Studio по режимам — см. `scripts/robokassa.py::PLAN_CATALOG`, без автопродления) и то, что у Robokassa
  `/Merchant/Recurring` нет тестового режима и требует отдельного
  согласования, делают его отдельной будущей задачей.

## Update: per-plan entitlements (migration 016)

- **Было:** любая оплата → `users.plan_type = 'pro'` → безлимит на все режимы
  без проверки длины исходника, хотя оферта и UI описывали тарифы с лимитами.
- **Стало:** `scripts/entitlements.py` + таблица `user_subscriptions`: каждая
  покупка — период с `plan_id`, режимом (cut / trailer / streamer),
  `ops_limit` / `ops_used` и `max_source_sec`. Списание операции — атомарно с
  созданием job в `create_job_with_quota` (под row-lock пользователя; replay
  по Idempotency-Key и откат INSERT не списывают). Длина исходника меряется
  `ffprobe` на сервере после upload/скачивания, до S3 и до списания.
- **Legacy Pro (500 ₽):** не мигрирован на новые тарифы — работает как продан
  (безлимит до `plan_active_until`), затем пользователь — обычный free.
- **Деплой:** на API-сервере нужен `ffmpeg` (ffprobe) — проверяется
  `prod_preflight_check.py backend`; без него платные job отклоняются (503).

## Update: payment provider decoupled — Robokassa is legacy

- SONYA больше не принимает оплату через Robokassa; основной провайдер —
  Самозанятые.рф (интеграции в репозитории пока нет — нет API-контракта).
- Тарифы не зависят от провайдера: каталог — `scripts/pricing.py`, права —
  `scripts/entitlements.py`, провайдер — `scripts/payment_providers.py`
  (env `PAYMENT_PROVIDER`; пусто → checkout 503 `payment_unavailable`, без
  создания платежа). `payments.provider` фиксирует, кто может подтвердить
  платёж; legacy ResultURL Robokassa отклоняет чужие платежи.
- Robokassa: `scripts/robokassa.py`, `RobokassaProvider`,
  `/api/billing/robokassa/result` — legacy, оставлены только для
  подтверждения уже созданных Robokassa-платежей; план удаления — после
  проверки, что в production-БД нет pending Robokassa-платежей.
