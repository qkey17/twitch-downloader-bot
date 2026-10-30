# Local Bot API: подготовка без переключения production

**Никакого production-переключения до согласования следующего этапа.** Этот PR
не устанавливает бинарник/unit на Oracle, не создаёт пользователя, не меняет
credentials, endpoint, polling, UI, drop_pending_updates или deployment.

## Подготовленная версия и сборка

Официальный [tdlib/telegram-bot-api](https://github.com/tdlib/telegram-bot-api),
Bot API **10.3**, commit `e3e9dd8e5b3d7ab8537cd5a10dc31d5ffa8f82d1`.
TDLib gitlink: `bc9c263e2bfee06aaab41e82db51a103376030bc`.
Все вложенные submodules берутся по gitlinks закреплённого дерева и записываются
в `upstream-revisions.txt`. Плавающие master/latest для сборки не используются.

Workflow [Build Local Bot API](../.github/workflows/build-local-bot-api.yml)
работает на GitHub-hosted **Ubuntu 24.04 x86_64**, не на Oracle. PR-trigger
проверяет только связанные пути; после появления workflow в main доступен ручной
workflow_dispatch. Нет production environment, secrets, SSH, deployment или
pull_request_target. GITHUB_TOKEN имеет только contents:read; checkout не
сохраняет credential. Официальные actions закреплены SHA.

Сборка `deploy/local-bot-api/build.sh` использует Clang, CMake/Ninja, OpenSSL,
zlib, gperf, Release, два параллельных задания. Проверяются точные source/gitlink,
ELF x86-64, `ldd` без missing libraries, `--help`, `--version`, используемые
флаги и SHA256. Реальные api_id/hash или bot token не используются. CI проверяет
shell syntax/ShellCheck и `systemd-analyze verify`, но не активирует службу.

Из успешного run скачать artifact
`telegram-bot-api-10.3-ubuntu24.04-x86_64` через GitHub Actions UI или `gh run download`.
Проверить репозиторий/run/head SHA и build-info.txt; после распаковки на
совместимом Linux выполнить `sha256sum --check SHA256SUMS`, `file telegram-bot-api`,
`ldd telegram-bot-api`, затем `./telegram-bot-api --help`. Сохранить SHA256/run ID
в записи будущей установки: artifact хранится 30 дней. Checksums выявляют
повреждение, но не заменяют доверие к проверенному workflow и происхождению.

`runtime-dependencies.txt` и build-info.txt содержат фактические библиотеки и
версии пакетов сборочного образа. На Oracle нужны совместимые runtime библиотеки
Ubuntu 24.04, прежде всего OpenSSL 3, zlib, libc/libstdc++ по `ldd`; компилятор,
gperf, CMake и dev packages не нужны. Зависимости и glibc baseline сверить перед
установкой: одинаковая ОС не заменяет проверки библиотек. Никакие пакеты на
Oracle этим workflow не устанавливаются. Бинарник в Git не коммитится.

## Будущая установка — только с новым разрешением

Образец: [tdb-local-bot-api.service](../deploy/systemd/tdb-local-bot-api.service).
Планируемые места: binary `/usr/local/libexec/tdb-local-bot-api/telegram-bot-api`,
unit `/etc/systemd/system/tdb-local-bot-api.service`, state/files/tmp под
`/var/lib/tdb-bot-api`. Отдельный системный user/group `tdb-bot-api`, без login/sudo.
StateDirectory создаёт каталоги 0700; temp находится на диске, не в tmpfs.
Unit запускается с `--local`, bind **127.0.0.1:8081**, без statistics listener.
Никаких Nginx upstream, публичных портов или изменений firewall не требуется.

Root-owned `/etc/tdb-local-bot-api/api.env` (0600, защищённый parent) должен позже
содержать только `TELEGRAM_API_ID` и `TELEGRAM_API_HASH` владельца. Это Telegram
application credentials, не Twitch credentials и не bot token. Сейчас файла и
значений не создаём и не запрашиваем. Systemd читает EnvironmentFile до sandbox;
сам каталог скрыт от процесса. Не передавать secrets через ExecStart/CLI,
не использовать весь TCD .env. Bot token в этот файл не нужен: его позже передаст
TCD в запросе при отдельно согласованной миграции. State TDLib тоже чувствителен.

ProtectHome скрывает TCD .env, SQLite, backup/offsite и SSH в /home. Root filesystem
read-only; запись разрешена в StateDirectory и private tmp. HTML root read-only.
Root/binary/unit нельзя отдавать на запись service user. Loopback не защищает от
других локальных процессов: token и state должны оставаться закрытыми.

Для чтения HTML позже предоставить службе минимальное **чтение/проход**, например
через отдельную группу/ACL с наследованием для новых публикаций после проверки
реальных mode/ownership. Не добавлять пользователя в ubuntu/sudo и не делать
HTML world-readable. ReadOnlyPaths не предоставляет недостающие Unix-права и
сам по себе не ограничивает чтение всех остальных общедоступных системных файлов.
Службе не нужен доступ на запись к результатам, БД, Nginx или backup.

Unit не связан с TCD через PartOf/Requires/BindsTo и не меняет boot recovery:
backup должен штатно останавливать TCD, не перезапуская Local API. Restart при
сбое ограничен тремя попытками за пять минут. SIGTERM завершает upstream штатно;
после 120 секунд systemd не посылает SIGKILL: зависшую остановку разбирать вручную,
не удалять state и не запускать вторую инстанцию. API stdout/stderr отключены,
чтобы исключить запись URL с токеном/TDLib dumps; доступны systemd lifecycle,
exit status, cgroup counters. Расширенная диагностика требует отдельного плана
редактирования/защиты логов, не включать verbosity с production-токеном вслепую.

## Готовность и отказ

До использования production-бота отдельно проверить user/permissions, целостность
binary, зависимости, незанятость порта, синтаксис unit, наличие credentials,
ограничения доступа, штатный SIGTERM и restart на безопасном стенде. Если есть
уже разрешённый тестовый бот, проверить его отдельно; не создавать credentials
автоматически. `--help` не запускает рабочую TDLib bot session и не измеряет её RAM.

Будущая readiness имеет два уровня: служба active и listener исключительно
127.0.0.1 (проверка `ss`), затем успешный getMe и приём updates **только после
разрешённого входа соответствующего бота**. Открытый TCP-порт не доказывает
работоспособность Telegram. Не вызывать getMe с production-токеном при подготовке:
это может авторизовать бота на новом сервере до cloud logOut.

При отказе проверять exit status, restart budget, OOM counters, свободный диск,
зависимости, права credentials/state и listener; не печатать environ или api.env.
Не удалять TDLib session, не вызывать logOut/close, не переключаться автоматически
в cloud и не повторять отправки после неопределённого сетевого исхода. Полная
миграция/rollback здесь намеренно не автоматизированы. Действующий Oracle deployment
не устанавливает эту службу; будущую однократную установку согласовать отдельно.

## Ограничения и следующий этап

MemoryHigh=128M, MemoryMax=256M, MemorySwapMax=64M, CPUQuota=50%, TasksMax=128 —
начальные предохранители для наблюдаемых испытаний, не измеренная потребность и
не гарантия загрузки 2000 MiB. На Oracle всего около 1 GiB RAM. До переключения
нужны реальные idle/authenticated/transfer memory, CPU/disk/network measurements,
проверки OOM/ENOSPC/обрывов и влияния на TCD. Пределы при необходимости пересмотреть
только по результатам и с разрешения владельца. Не ставить компиляторы на Oracle.

Следующий план: (1) PTB cloud/local configuration с прежним default и тестами;
(2) отдельно согласованная установка/безопасные ресурсные испытания;
(3) отдельное окно переключения с cloud logOut, единственным endpoint/poller,
политикой pending updates и планом возврата; (4) после стабильности — доставка
HTML. [Telegram](https://core.telegram.org/bots/api#logout) запрещает возврат в cloud
10 минут после logOut; текущий drop_pending_updates=True нельзя менять молча.

Для будущего sender нужны Path/file URI без чтения целого HTML в RAM Python,
длительные send-specific timeouts, защита ready HTML от cleanup и учёт отправок
в maintenance/graceful shutdown. Политику параллельных отправок согласовать отдельно;
существующий глобальный VOD-слот не менять. UI и SQLite на этом этапе не меняются.

**Offsite budget сейчас 1,5 млрд байт.** Будущий почти 2 GiB HTML не обязан сжаться
до этого бюджета; рост snapshot size/time и локальных копий требует отдельного
решения по backup. Резерв 10 GiB, backup/offsite timeouts и доступное место также
перепроверить. Ни backup/recovery, ни его лимиты данным PR не изменяются.
