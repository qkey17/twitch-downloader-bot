# Изолированный integration harness PR #4

Статус: **подготовлен, реальный Linux/systemd-прогон ещё не выполнен**. Это отдельная
тестовая инфраструктура, не способ установки автоматизации в production.
Исходный контроллер и maintenance закреплены на `0a9a453aa975615df1c0c0b6ca835f277d7dfe1c`.
Production-файлы репозитория не меняются. Обновление исходной версии требует отдельно
пересмотреть pin, контракты и тесты стенда.

## Изоляция и границы проверки

Используется новая одноразовая **полная QEMU VM x86_64** на отдельном компьютере.
Собственные kernel, PID 1/systemd, root ext4, `/run`, `/home` и system bus.
Launcher задаёт `-nodefaults -nic none`, не принимает произвольных QEMU-аргументов,
не подключает рабочие каталоги, host devices, systemd/D-Bus sockets или сеть.
Передача исходников — только allowlist через read-only ISO. Не используйте Docker,
chroot, пользовательский systemd, привилегированный контейнер или копию Oracle-диска.
Параметры изоляции соответствуют [документации QEMU](https://www.qemu.org/docs/master/system/qemu-manpage.html).

Внутри VM root-driver проверяет DMI `TDB-Harness / TDB-Isolated-VM`, root filesystem
с меткой `TDB-HARNESS` и типом ext4, PID namespace, PID 1 `systemd`, peer PID 1/UID 0
у `/run/systemd/private`, отсутствие контейнера, общих файловых систем и всех NIC
кроме loopback. Требуются Ubuntu 24.04, Python 3.12; provisioning требует systemd 255.
При наличии известных production-путей или существующих TDB units/user он откажет.
Последующие запуски проверяют root-owned attestation, связанную с machine-id.
Непривилегированные fixture-процессы доверяют этой attestation; доступ к private
manager socket и чужим `/proc/1/ns` проверяет root-driver.

Эти проверки защищают от ошибочного запуска, а не от root, намеренно подделывающего
окружение. **Основание изоляции — полный отдельный guest и фиксированный launcher**.
Не переносите harness в существующую VM с данными и не обходите предохранители.

В гостевом `/etc/systemd/system` создаются только четыре тестовых unit с точными
именами, нужными контроллеру. Используются реальный `/usr/bin/systemctl`, jobs,
`sudo -n` с двумя точными командами start/stop синтетического `tdb.service`,
`ExecStopPost`, `OnFailure`, enabled boot recovery и `ensure_supervised()` без изменений.
Тестовый бот импортирует настоящий `src/maintenance.py`, включая durable commit,
gate, peer credentials и наследование маркера. Telegram/Twitch не импортируются.

Тестовые admin wrappers импортируют исходные вспомогательные функции backup/offsite,
а **CLI snapshot/verify/offsite заменены**. Создаются крошечные синтетические SQLite,
HTML и checksum; результат помечен `synthetic-harness-only`, это **не** snapshot
`tdb-backup-v1`. Offsite пишет только локальные события и возвращает контрактный
`cloud_verified=true, synthetic_only=true`. Нет rclone, age, Dropbox, архива или сети.
Стенд проверяет оркестрацию, не проверяет production backup/restore/encryption/upload.

В случае 11 adapter инъецирует `OSError` только в `Runtime.service()` реального
контроллера; успешные чтения и остальные команды идут настоящему manager. Это
проверка реакции на ошибки чтения, **не** испытание отказа настоящего D-Bus/systemd.

## Ресурсы

Guest: 1 vCPU, 954 МиБ RAM, новый sparse qcow2 32 ГиБ. Контроллер сохраняет production
нижний порог свободного диска **10 ГиБ**. Каждый тестовый service: `CPUQuota=50%`,
`MemoryHigh=96M`, `MemoryMax=160M`, `TasksMax=32`, `Nice=10`. Три service вместе
ограничены 480 МиБ; ОС и root-driver расходуют память дополнительно.

На отдельном хосте планируйте минимум около 2 ГиБ свободной RAM для QEMU+guest и
достаточно диска для роста qcow2/ISO. Это оценка, **не измерение**. При обнаружении
известных production-путей launcher откажет; на Linux он также требует не менее
2 ГиБ `MemAvailable` перед init/run. На Oracle с 954 МиБ RAM запуск дополнительной
VM вместе с production создаёт риск OOM/swap;
здесь он не предусмотрен. TCG не требует nested virtualization, но заметно медленнее
аппаратной виртуализации. Timeout теста на медленном хосте — FAIL, не доказательство
дефекта контроллера; разберите журнал. Реальные MemoryPeak/CPUUsageNSec пишутся в отчёт.

## Подготовка — только отдельный тестовый компьютер

Нужны Python 3, Git с указанным commit, QEMU/qemu-img, Ubuntu Server 24.04 x86_64 ISO
с Python 3.12, systemd 255, sudo, и утилита создания ISO (например xorriso).
Не устанавливайте ничего на Oracle. Проверьте подлинность Ubuntu ISO обычным способом.
Стенд не устанавливает пакеты и не скачивает образы. Все команды ниже выполняются
в новой тестовой среде; не переносите их в production SSH-сессию.

На тестовом компьютере из checkout новой тестовой ветки (пути примеров относительны
checkout, каталог `_work` уже существует или создаётся вручную):

```sh
python tests/systemd_harness/host.py bundle _work/tdb-harness-payload
xorriso -as mkisofs -V TDB_HARNESS -o _work/tdb-harness-payload.iso _work/tdb-harness-payload
python tests/systemd_harness/host.py init _work/tdb-harness-vm
python tests/systemd_harness/host.py plan _work/tdb-harness-vm --iso /absolute/path/ubuntu-24.04-live-server-amd64.iso --install --gui
python tests/systemd_harness/host.py run _work/tdb-harness-vm --iso /absolute/path/ubuntu-24.04-live-server-amd64.iso --install --gui
```

`bundle` и `init` требуют **новый** destination, никогда не перезаписывают его.
Bundle содержит только четыре исходных файла audited commit, четыре модуля harness
и manifest SHA-256. Нет `.git`, `.env`, данных, credentials и зависимости от секретов.
На Windows эквивалентные команды выполняйте с доступным Python/QEMU; xorriso нужен
на отдельной рабочей машине, это не новая runtime dependency TCD. Не заменяйте ISO
на общий каталог с хостом.

Установите Ubuntu в **единственный новый виртуальный диск**, без NIC, LVM, шифрования
или общих mount. Выберите обычную ext4 root partition, метка **TDB-HARNESS**.
При необходимости задайте метку средствами installer; для уже установленной новой
VM — только из её консоли после проверки `findmnt /`/`lsblk`, метка root ext4.
Harness сам диски не форматирует и не меняет метки. Не используйте настоящие учётные
данные, Oracle/rclone-конфигурацию, ключи или production backup. Для входа создайте
тестового администратора (не `tdbh`). Если installer требует сеть для пакетов,
подготовьте offline media; не добавляйте NIC и не ослабляйте guard.

Выключите установленный guest штатно, затем запустите с payload ISO:

```sh
python tests/systemd_harness/host.py run _work/tdb-harness-vm --iso _work/tdb-harness-payload.iso --gui
```

Внутри **консоли новой VM**:

```sh
sudo mkdir /mnt/harness
sudo mount -o ro /dev/sr0 /mnt/harness
sudo /usr/bin/python3 -I -S -B /mnt/harness/guest.py provision
sudo /usr/bin/python3 -I -S -B /var/lib/tdb-harness/code/guest.py check
sudo systemd-analyze verify /etc/systemd/system/tdb.service /etc/systemd/system/tdb-daily-backup.service /etc/systemd/system/tdb-backup-recover.service /etc/systemd/system/tdb-daily-backup.timer
```

Не выполняйте последнюю команду при отказе guard/provision. При частичном provisioning
сохраните диагностику и начните с новой одноразовой VM; не удаляйте неизвестные файлы
ради обхода collision checks. После успешного provisioning ISO больше не нужен:
код установлен root в `/var/lib/tdb-harness/code`, admin и данные принадлежат `tdbh`.

## Запуск и сценарии

В консоли этой VM, вне 23:00–00:00 UTC (чтобы тестовый busy-case не пересёк окно):

```sh
sudo /usr/bin/python3 -I -S -B /var/lib/tdb-harness/code/scenarios.py run
```

Выполняются последовательно, с конечными ожиданиями, следующие группы:

| № | Проверка с настоящим systemd |
|---|---|
| 1 | prepare → реальная остановка → synthetic snapshot/verify → запуск → socket readiness/stability → локальный offsite; повтор без нового snapshot |
| 2 | Busy VOD, отсутствие stop и owner, ограниченное наблюдение с SIGTERM |
| 3–5 | SIGTERM/SIGKILL daily controller после durable owner; SIGKILL при реальном pending stop job; ExecStopPost/OnFailure recovery |
| 6 | Отдельные ошибки snapshot и verify, восстановленный бот, отсутствие отправки |
| 7 | Бот не запускается; конечный отказ, сохранённые owner/attempts и critical status |
| 8 | Type=simple уже active, socket ещё не готов (4 секунды), owner сохраняется |
| 9 | SIGKILL синтетического бота во время recovery, настоящий Restart=on-failure, смена InvocationID/NRestarts |
| 10 | attempts=3: работающий готовый процесс восстанавливается без нового запуска; остановленный — отказ без расхода сверх лимита |
| 11 | Однократный/постоянный сбой чтения (явная инъекция), manager и прочие команды реальные; owner/бюджет сохраняются |
| 12 | Recovery и TDB запускаются одним manager transaction без взаимного ожидания |
| 13 | Настоящий Persistent timer catch-up с просроченным собственным stamp и параллельным recovery |
| 14 | Отдельная двухфазная проверка power loss/следующего boot, см. ниже |
| 15 | Manual hold, общий deployment flock, конкурирующий recovery, отказ второго unsupervised run |
| 16 | Барьер offsite: приложение уже готово, owner очищен, процесс работает |

Тестовый timer — каждую минуту для проверки catch-up; production timer и окно
15:00–18:00 UTC+5 не меняются. Ожидание ready 8 секунд, stability 1 секунда, stop 7
секунд; synthetic stop ≤5 секунд, барьеры обычно ≤25 секунд.
В случае постоянного start failure ready/stop limits уменьшены до 2 секунд,
поскольку recovery может вызываться последовательно из нескольких защитных hooks.
Длительные 30/180/500/800 секунд остаются в существующих unit-тестах с виртуальным
временем. Реальный suite
может занять несколько минут; бесконечных ожиданий нет. Внешний timeout запуска
делайте с запасом (например 15 минут), после прерывания выполните cleanup в этой VM.

`PASS` выдаётся только после assertions, итоговый `report-<uuid>.json` сохраняет
версию systemd/Python, исходный SHA, результаты и выбранные свойства реальных units.
Каждый case имеет собственные SQLite/HTML/state/locks/events; предыдущие артефакты
и owner сохраняются. В `finally` отключаются timer/boot units, останавливаются только
свои службы, проверяются jobs, MainPID/ControlPID и cgroup.procs. Без `cleanup=PASS`
прогон не считается завершённым. Ни один каталог рекурсивно не удаляется.

### Отдельный power loss/boot (№14)

После основного прогона в той же disposable VM:

```sh
sudo /usr/bin/python3 -I -S -B /var/lib/tdb-harness/code/scenarios.py stage-powercut
```

Команда ставит новый case на snapshot-барьер с fsynced owner/checkpoint и включает
**только гостевые** TDB/boot-recovery. Это незавершённая двухфазная проверка; cleanup
на этой точке намеренно отложен до boot. **В течение 10 секунд** в терминале QEMU
на тестовом компьютере нажмите Ctrl-a, c (monitor), введите `quit`. Завершается весь
этот QEMU guest без guest shutdown. Не используйте команды reboot/poweroff хоста,
Oracle или сигналы по произвольным PID. Барьер истекает через 60 секунд; пропущенный
powercut должен дать FAIL, не ложный PASS.

Снова `host.py run` с тем же private qcow2, без `--install`, затем в консоли VM:

```sh
sudo /usr/bin/python3 -I -S -B /var/lib/tdb-harness/code/scenarios.py resume
```

Проверяется новый kernel boot_id, автоматический boot recovery со старым owner,
готовность бота и отсутствие offsite. Пишется `reboot-result.json`, затем cleanup.
Основной отчёт сохраняет `reboot=NOT_RUN`; только отдельный `reboot-result.json=PASS`
подтверждает №14. При отказе от этой фазы сразу выполните cleanup в guest.

### Ошибки и завершение

При FAIL сохраните JSON reports, case events и guest journal соответствующих четырёх
units для анализа. Это только синтетические данные. Не собирайте host journal/env.
Не исправляйте harness удалением owner или заменой controller/gate в production.
Повтор suite создаёт новые cases; старые тестовые owner не сбрасываются.

Ручное завершение внутри разрешённой VM:

```sh
sudo /usr/bin/python3 -I -S -B /var/lib/tdb-harness/code/guest.py cleanup
```

При изменённых установленных файлах cleanup откажет. При зависшем guest/невозможности
cleanup выключите **только эту QEMU VM** через её monitor. Для окончательной очистки
можно удалить созданный лично для неё qcow2/ISO на тестовом компьютере после проверки
точных путей; harness ничего автоматически не удаляет и не содержит общего rm -rf.

## Что действительно проверено при разработке

Windows / Python 3.14: безопасные unit-тесты guard, allowlist/checksum, fixed QEMU argv,
структура unit-конфигурации, синтетический checksum/SQLite, порядок событий, наличие
сценариев и Python 3.12 grammar. Проверен реальный отказ guest entry points на Windows
до вызова manager/создания ресурсов (четыре entry points, exit 1). Выполнены 19/19
новых безопасных unit-тестов; полный TCD suite: 504 теста, OK, 15 пропусков из-за
Linux peer credentials и недоступного создания symlink на Windows. Compileall
`src tools tests` — OK. Собран настоящий allowlist-bundle из Git и проверены SHA-256
всех восьми файлов; вместе с manifest в нём девять файлов.

Не выполнены: запуск QEMU/Ubuntu, `systemd-analyze verify` и все реальные manager
сценарии, включая power loss. Локально нет Linux/WSL/QEMU. Поэтому работоспособность
harness на systemd 255, соответствие resource limits и production-совместимость
**ещё не подтверждены**. Первый запуск на отдельной VM — обязательный следующий этап.
Настоящие длительные VOD, HTML workers/cleanup, реальные backup/offsite и деградация
D-Bus требуют отдельных проверок; синтетический бот их не воспроизводит.

Семантика тестовых units опирается на документацию systemd 255:
[service / ExecStopPost / Restart](https://www.freedesktop.org/software/systemd/man/255/systemd.service.html),
[timer / Persistent](https://www.freedesktop.org/software/systemd/man/255/systemd.timer.html),
[systemctl / kill-whom](https://www.freedesktop.org/software/systemd/man/255/systemctl.html).
