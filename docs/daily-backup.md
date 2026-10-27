# Ежедневный автоматический backup TCD

Это инструкция для **последующей отдельно разрешённой установки и проверки**. Подготовка PR ничего не устанавливает на Oracle, не включает timer и не останавливает production. Сначала нужны review, merge и отдельное разрешённое развёртывание интеграции бота; существующий workflow `Oracle deployment` не изменяется.

## Как устроено

`tools/daily_backup.py` координирует существующие [backup/restore](backup-restore.md) и [offsite backup](offsite-backup.md). Systemd timer запускает его ежедневно в **10:00 UTC = 15:00 UTC+5**, независимо от часового пояса сервера. Если бот занят, попытка повторяется каждые 900 секунд до 13:00 UTC (18:00 UTC+5). После окончания окна день пропускается; очереди пропущенных дат нет. Перезапуск VM допускает одну актуальную попытку через `Persistent=true`, но не переносит старые backup за пределы окна. Повтор одного дня и параллельные экземпляры контролируются приватным журналом и блокировками.

Приватный Unix socket связывает контроллер с текущим процессом бота. Подготовка проверяет отсутствие активного VOD, закрывает приём новых обработок и дожидается завершения HTML cleanup. Другие уже запущенные Telegram-обработчики и принадлежащие им фоновые операции штатно завершаются через существующий PTB shutdown; создание snapshot начинается только после полного завершения службы. До подтверждения безопасного состояния systemd stop не вызывается. При обычном отказе/закрытии подготовительного соединения режим возвращается; после подтверждённого commit обслуживания gate остаётся закрыт до явного восстановления. Наличие файлов, время записи SQLite и одно только `systemctl active` не считаются доказательством простоя.

Commit дополнительно сохраняет приватный runtime marker. `RuntimeDirectoryPreserve=restart` удерживает его при автоматическом перезапуске TDB: новый процесс наследует закрытый gate и не начинает VOD перед уже поставленной остановкой. При обычной окончательной остановке systemd удаляет runtime directory; следующий разрешённый запуск после snapshot получает новый socket. После reboot `/run` пуст, старого stop job уже нет, а постоянный intent разбирает boot recovery. Runtime marker не заменяет постоянный журнал владения остановкой.

Далее контроллер: сохраняет намерение остановки → штатно останавливает `tdb.service` → подтверждает отсутствие процесса и stop job → создаёт уникальный snapshot через `backup_restore.py` → выполняет его verify → запускает TDB и подтверждает активность, новый процесс и готовность приложения через socket → проверяет стабильность → только после этого вызывает `offsite_backup.py`. Проблемы Dropbox уже не удерживают бот остановленным. Snapshot остаётся полным `tdb-backup-v1`: SQLite, HTML, история и метаданные не редактируются. Дедупликация и локальная ротация не выполняются.

## Восстановление после сбоя

Приватный постоянный state хранит, **что именно эта процедура получила право восстановить ранее работающий бот**. Служба, остановленная до процедуры, не запускается автоматически. Перед остановкой записывается durable intent; перед каждой попыткой старта сохраняется счётчик. Бесконечных повторов нет: default — максимум 3 попытки. Не удаляйте журнал или lock для обхода ошибки.

Обычный путь восстановления дополнен `ExecStopPost` ежедневного unit: его выполняет systemd после ошибки, timeout или гибели основного процесса, включая SIGKILL. Все дочерние процессы backup должны завершиться перед recovery. Отдельный `tdb-backup-recover.service` вызывается при ошибке unit и включается на boot для разбора оставшегося intent после power loss. Он не создаёт новый backup и не обращается к Dropbox. На штатном shutdown сервисы останавливаются; следующий boot выполняет восстановление по сохранённому праву, а не по догадке о причине неактивности.

**Граница гарантии:** ни Python, ни systemd не могут гарантировать работающий бот при неисправном приложении, повреждённом диске, недоступном manager или изменении службы посторонним администратором. После исчерпания попыток сохраняется критический статус, требующий ручного вмешательства. Произвольный `systemctl stop` вне общего протокола нельзя надёжно отличить от остановки автоматизацией. Ручные работы обязаны использовать описанный ниже hold и общую deployment-блокировку.

Текущий бот запускается с `drop_pending_updates=True`: Telegram updates, накопившиеся во время краткой остановки, могут быть отброшены существующим поведением запуска. Этот PR не меняет Telegram interface и не обещает доставку таких updates. Активный VOD ради backup не прерывается.

## Подготовка и установка на Oracle

1. Сначала проверяйте на изолированном Linux/systemd стенде с синтетическими SQLite/HTML и отдельной тестовой службой. Не используйте production database для проверки SIGKILL, timeout или отказа запуска.
2. После review/merge установите утверждённую интеграцию бота отдельным разрешённым deployment. Сохраните действующие `EnvironmentFile`, `ExecStart`, пользователя службы, SQLite и HTML. Нужны существующие graceful guards: `Type=simple`, `KillSignal=SIGTERM`, `KillMode=control-group`, `SendSIGKILL=no`, `TimeoutStopSec=infinity`. Контроллер проверяет их, а не исправляет сам.
3. Под пользователем `ubuntu` подготовьте приватные каталоги `/home/ubuntu/tdb-admin`, `/home/ubuntu/tdb-backups`, `/home/ubuntu/tdb-offsite`, `/home/ubuntu/tdb-daily-state` (0700). Не очищайте существующие каталоги или offsite state. Перенесите проверенные `daily_backup.py`, `backup_restore.py`, `offsite_backup.py` **одной версии** рядом в `tdb-admin` с правами 0600. Скрипты запускаются через `/usr/bin/python3`, executable bit не нужен.
4. Скопируйте `deploy/daily-backup.example.json` в новый `/home/ubuntu/tdb-admin/daily-backup.json`, права 0600. Проверьте все абсолютные пути и HTML roots. Не редактируйте конфигурацию, не заменяйте скрипты и не перемещайте state, пока процедура или recovery выполняются: recovery обязан получить те же пути. Файл не содержит секретов; `.env` не копируется. Убедитесь, что `python3`, age и rclone доступны из `PATH=/usr/sbin:/usr/bin:/sbin:/bin` и существующий Dropbox remote настроен для `ubuntu`. Закрытый ключ age остаётся исключительно на Windows.
5. Сохраните минимум **10 ГиБ** свободного места на filesystem исходных данных/backup/offsite. Лимит не разрешается уменьшать ниже эксплуатационного минимума; накопление локальных snapshots и ciphertext нужно контролировать вручную. Облачные параметры остаются прежними: максимум 3 управляемых архива, бюджет 1 500 000 000 байт, account reserve 300 000 000 байт. Старая и новая копии должны помещаться одновременно. `first-snapshot.tar.gz.age` не становится управляемым автоматически.
6. Используйте существующие точечные sudo-разрешения `ubuntu` на `/usr/bin/systemctl --no-block stop tdb.service` и `/usr/bin/systemctl --no-block start tdb.service`. **Не выдавайте sudo на Python или редактируемый административный скрипт целиком.** Unit запускается как `ubuntu`. `NoNewPrivileges=yes` несовместим с этим ограниченным sudo-путём, поэтому в образцах не задан.
7. Проверьте, что новых имён unit/drop-in ещё нет; при наличии сначала разберите установленную версию. Следующие команды выполняет владелец **позднее**, из проверенного checkout, после отдельного разрешения:

   ```sh
   sudo install -m 0644 deploy/systemd/tdb-daily-backup.service /etc/systemd/system/tdb-daily-backup.service
   sudo install -m 0644 deploy/systemd/tdb-daily-backup.timer /etc/systemd/system/tdb-daily-backup.timer
   sudo install -m 0644 deploy/systemd/tdb-backup-recover.service /etc/systemd/system/tdb-backup-recover.service
   sudo install -d -m 0755 /etc/systemd/system/tdb.service.d
   sudo install -m 0644 deploy/systemd/tdb.service.d/30-maintenance.conf /etc/systemd/system/tdb.service.d/30-maintenance.conf
   sudo systemd-analyze verify /etc/systemd/system/tdb-daily-backup.service /etc/systemd/system/tdb-daily-backup.timer /etc/systemd/system/tdb-backup-recover.service
   sudo systemctl daemon-reload
   sudo systemctl enable tdb-backup-recover.service
   ```

   Timer пока **не включён**. Drop-in добавляет только opt-in socket, приватный `RuntimeDirectory` и сохранение runtime marker при restart; он начинает действовать при следующем разрешённом запуске TDB. Согласуйте применение drop-in с отдельным deployment; не вызывайте restart рабочего бота ради этой инструкции. Существующие SSH, Nginx, Actions и production credentials не меняются.

## Проверка расписания и тестовый запуск

```sh
systemd-analyze calendar '*-*-* 10:00:00 UTC'
systemctl cat tdb-daily-backup.timer
/usr/bin/python3 -I -S -B /home/ubuntu/tdb-admin/daily_backup.py --config /home/ubuntu/tdb-admin/daily-backup.json status
```

Ожидается следующее срабатывание в 10:00 UTC. После отдельной проверки можно включить timer: `sudo systemctl enable --now tdb-daily-backup.timer`, затем `systemctl list-timers --all tdb-daily-backup.timer`. Часовой пояс сервера менять не требуется.

Для первого разрешённого полного прогона используйте `sudo systemctl start tdb-daily-backup.service` **внутри окна**. Он действительно остановит бот только после согласования и создаст/отправит snapshot; это не dry-run. Вне окна контроллер должен безопасно пропустить запуск. На первом прогоне наблюдайте из отдельной SSH-сессии, не запускайте параллельно deployment или ручной backup. После успеха отдельно скачайте, расшифруйте и восстановите облачную копию по [инструкции offsite](offsite-backup.md); unit tests и exit 0 не заменяют независимую проверку реальных данных.

Расписание меняется drop-in timer с очисткой старого `OnCalendar=` и новой явной UTC датой/временем; после него нужны `daemon-reload` и перезапуск **timer**, не TDB. `window_start_utc`, `window_end_utc` и `retry_seconds` изменяются в JSON согласованно с timer. При увеличении тайм-аутов увеличьте соответствующие `TimeoutStartSec`/`TimeoutStopSec` образцов; default ежедневного unit — 5 часов, recovery — 30 минут.

## Успешность, журнал и действия после сбоя

Смотрите безопасный статус контроллера и unit, а не одно наличие каталога snapshot:

```sh
/usr/bin/python3 -I -S -B /home/ubuntu/tdb-admin/daily_backup.py --config /home/ubuntu/tdb-admin/daily-backup.json status
systemctl show tdb.service --property=ActiveState --property=SubState --property=MainPID --property=Result --property=ExecMainStatus --property=NRestarts
systemctl show tdb-daily-backup.service --property=Result --property=ExecMainStatus
sudo journalctl -u tdb-daily-backup.service -u tdb-backup-recover.service --since today --no-pager
```

В journal пишутся только стадии, фиксированные коды ошибок и безопасные метаданные. Не публикуйте полный journal TDB, `.env` или `rclone.conf`. Успех требует проверенного snapshot, восстановленного TDB и успешного полного сравнения облачного архива. Busy/window skip отличается от успешного backup. Незавершённый snapshot-кандидат и pending offsite сохраняются отдельно от суточного результата. Повторная попытка сначала проверяет допустимость кандидата и готовность TDB; проверенный кандидат можно отправить без новой остановки. Для неопределённого результата offsite используется подтверждённый job ID из прежнего журнала, включая уже проверенную облачную копию: совпадение имени не даёт права считать отправку завершённой. Очередь ограничена начатой pending-отправкой и последним кандидатом. При длительной недоступности Dropbox промежуточные кандидаты могут быть заменены более свежим: не каждый локальный snapshot такого периода обязательно отправится позже. Все локальные каталоги сохраняются. При ошибке Dropbox сохраните snapshot и постоянный offsite state; ручной resume также возможен по существующей инструкции, когда TDB уже работает.

При критическом recovery failure сначала проверьте состояние `tdb.service`, наличие manual hold и незавершённой административной операции. Не удаляйте intent, lock или snapshot и не запускайте второй процесс бота вручную. Владелец должен исправить конкретную причину запуска и затем через обычную согласованную процедуру подтвердить готовность. Исчерпанный recovery budget требует ручного решения; очередной timer не должен скрыто обнулять счётчик.

Если исправленная служба всё ещё остановлена после исчерпания budget, владелец сначала отключает timer и завершает административные units, затем под общей deployment-блокировкой выполняет обычный `systemctl start tdb.service`. После подтверждения готовности запускается `daily_backup.py --config /home/ubuntu/tdb-admin/daily-backup.json recover`: он может снять intent по фактически работающему приложению без новой попытки старта. Не изменяйте счётчик или параметры конфигурации для обхода лимита. Во время shutdown VM восстановление откладывается до boot, сохраняя intent и оставшиеся попытки.

Resource guards действуют только на административный cgroup: Nice 10, CPUQuota 50%, MemoryHigh 256 МиБ, MemoryMax 384 МиБ. Они не ограничивают cgroup TDB. `OOMPolicy=stop` завершает административный unit при OOM дочернего процесса, чтобы systemd выполнил recovery. Это начальные параметры для отдельного измерения на Oracle 954 МиБ RAM; OOM не считается успешным backup. Настройки rclone/age и потоковые буферы сохраняются из offsite-инструмента.

## Пауза и возврат к ручной процедуре

1. `sudo systemctl disable --now tdb-daily-backup.timer` прекращает новые срабатывания, но само по себе не отменяет работающий backup.
2. Дождитесь окончания `tdb-daily-backup.service` либо штатно остановите **этот административный unit**: `sudo systemctl stop tdb-daily-backup.service`. Его `ExecStopPost` должен восстановить TDB, если остановка принадлежала автоматизации. Проверьте status и готовность бота. Не применяйте SIGKILL к `tdb.service`.
3. Выполните `daily_backup.py --config /home/ubuntu/tdb-admin/daily-backup.json hold` от `ubuntu`. Hold использует общий административный lock; если операция ещё владеет им, безопасно дождитесь её окончания. Только после подтверждения hold переходите к намеренной ручной остановке TDB. Все ручные stop/start/backup должны также держать `/home/ubuntu/.local/state/tdb-deploy/deploy.lock`; существующий deployment helper уже это делает.
4. Выполните прежнюю [ручную maintenance-процедуру](backup-restore.md), сохраняя тот же offsite state. Не очищайте snapshots, ciphertext или job journals. Recovery unit оставьте установленным, пока есть неразобранный intent автоматизации.
5. Для возврата убедитесь в активном и готовом TDB, завершённых ручных работах и отсутствии старого intent. Выполните `daily_backup.py --config /home/ubuntu/tdb-admin/daily-backup.json unhold`, затем включите timer. Hold сохраняется после reboot; не удаляйте его файл вручную.

Копирование unit-файлов и `daemon-reload` не доказывают корректную recovery-процедуру. Перед production отдельно проверить на стенде: активный VOD и новая задача в момент подготовки, HTML cleanup/worker thread, завершение окна, stop/backup/verify/start failure, SIGTERM/SIGKILL контроллера, reboot с intent, manual hold, повтор/параллельный запуск и недоступный Dropbox. Только после этого разрешать один наблюдаемый production backup в согласованное окно, проверить возвращение бота **до** передачи и независимо восстановить загруженную копию.

Основание systemd-схемы: [ExecStopPost и OOMPolicy в systemd v255](https://github.com/systemd/systemd/blob/v255/man/systemd.service.xml), [таймеры и Persistent](https://github.com/systemd/systemd/blob/v255/man/systemd.timer.xml), [OnFailure и ограничения запусков](https://github.com/systemd/systemd/blob/v255/man/systemd.unit.xml), [RuntimeDirectoryPreserve](https://github.com/systemd/systemd/blob/v255/man/systemd.exec.xml). Эти механизмы не заменяют устойчивый журнал владения и проверку готовности приложения.
