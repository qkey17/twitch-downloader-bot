# Автоматические испытания PR #4 на настоящем systemd

Основной способ — проверка **Isolated systemd integration** в GitHub Actions.
Push в ветку автоматизации/стенда или обновление соответствующего PR запускает весь
цикл. Для повторения достаточно **Re-run all jobs** у этого прогона. `Run workflow`
становится доступен после регистрации workflow в default branch; до этого работает
обычный PR/push trigger. Workflow `Oracle deployment` не вызывается и не меняется.

Ожидаемый результат — Job summary с таблицей сценариев и архив
`systemd-integration-<run-id>-<attempt>`. В нём `report.md`, `summary.json`, console/QEMU
logs и отдельные JSON/events/journal для suite, powercut и следующей загрузки.
Статус PASS требует всех 01–13 и 15–16, подтверждённого сценария 14 и очистки.

## Что выполняется без участия пользователя

1. На временном CI-хосте устанавливаются QEMU/qemu-img. В guest пакеты не скачиваются.
2. Загружается Ubuntu 24.04 cloud image release-20260801, сверяется закреплённый SHA-256
   `0533b0655c32e68b31d792ecd6ccfca95abdbc536c4446874fe0513bd4140ffe` из
   [официального списка Ubuntu](https://cloud-images.ubuntu.com/releases/noble/release-20260801/SHA256SUMS).
3. Создаются новый sparse overlay 32 ГиБ и read-only ISO: payload и NoCloud seed.
   Контроллер — точный `75d16545c5ccbdb965b7dfd2cd73dd605f6f2e0d`; harness экспортируется
   из зафиксированного Git HEAD. Dirty checkout не принимается.
4. Headless QEMU загружает собственную Ubuntu/systemd 255, 954 МиБ RAM, 1 vCPU.
   NoCloud запускает проверенный bootstrap без логина, SSH и клавиатуры.
5. Bootstrap доказывает DMI, PID 1, namespace, peer credentials, отсутствие NIC/shared
   mounts и точный root `/dev/vda1` ext4. Только в этом новом guest задаёт root label
   `TDB-HARNESS`, согласуя fstab/GRUB с новой меткой. Затем действует обычный guard.
6. Устанавливается старый **проверенный** c77b5f7/0a9a453, выполняется его cleanup,
   затем настоящий updater заменяет обе копии контроллера и metadata до 75d1654.
   Это также проверяет описанный отказ updater на реальном systemd. Старые/новые
   исходники берутся из Git и проходят SHA-256, произвольные scripts не добавляются.
7. Запускается полный существующий suite, включая реальные transitions/jobs,
   Unix socket, SIGTERM/SIGKILL и Restart=on-failure. Сценарий 09 остаётся **8/1 с**.
   В сценарии 13 минутный тестовый timer выключается после первого offsite. Если он
   уже успел поставить повтор на границе минуты, допускается `previous_offsite_verified`:
   полный первоначальный lifecycle и ровно один snapshot всё равно обязательны.
   В 16 ожидание `offsite_begin` ограничено существующим общим пределом цикла 65 с:
   событию предшествуют stop, backup, verify, recovery и ещё один verify. На реальном
   TCG это заняло около 22 с, поэтому прежние 15 с давали ложный отказ наблюдателя.
   Пределы команд/готовности контроллера при этом не изменяются.
8. Сценарий 14 сохраняет owner/checkpoint, fsync и sync. HOST принудительно убивает
   **весь созданный им процесс QEMU**, не вызывает shutdown/reboot/service restart guest.
9. Повторно загружается **тот же overlay** с тем же UUID и seed. Проверяются новый
   kernel boot_id, boot recovery, owner/readiness, отдельный reboot-result и cleanup.
10. После передачи диагностики guest штатно выключается. Host всегда закрывает
    канал и завершает только собственный QEMU, в том числе при ошибках/timeout.

Guest RTC намеренно фиксирован на 2030-01-01 12:00 UTC: суточное тестовое окно не
зависит от времени запуска CI и не пересекает полночь. Длительности проверяются
настоящими монотонными часами. Часы Windows/CI/Oracle и production timer не меняются.

## Изоляция и ресурсы

Нет guest NIC (`-nic none`), login, графического интерфейса, SSH, shared folders,
host filesystem/bus passthrough, credentials, `.env`, настоящих SQLite/HTML/backup.
Существующий диск `C:\TCD-Systemd-Test\vm\guest.qcow2` вообще не используется.
Каждый прогон получает новый каталог и новый одноразовый диск.

Единственный управляющий канал — отдельный virtio-serial byte stream, backend
которого доступен только на host loopback 127.0.0.1. Это не NIC гостя и не shell:
host принимает только ограниченные JSON-события и allowlisted артефакты. Есть run ID,
ограничения размеров/общего объёма, SHA-256, защита от traversal/symlink/перезаписи.
Поступившие из guest файлы никогда не исполняются на хосте.

Root label/NoCloud применяются только к новой checksum-проверенной Ubuntu image.
Все исходные проверки guard сохранены. Root, намеренно подделывающий окружение или
нарушающий процедуру, вне модели угроз. Никакие команды не отправляются на Oracle.

По [документации GitHub](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
`ubuntu-24.04` предоставляет public job 4 CPU/16 ГиБ, private job 2 CPU/8 ГиБ и
14 ГиБ SSD. Это достаточно для 954 МиБ guest и sparse overlay с малыми синтетическими
данными. Предварительно требуется 2 ГиБ свободной RAM и 8 ГиБ свободного диска.
KVM используется только если доступен; иначе TCG. Наличие nested virtualization
не предполагается обязательным. Глобальный runner timeout — 30 минут, job — 45 минут;
первый boot handshake — до 8 минут, повторный — до 5 минут, загрузка image — до 8 минут. Это пределы ожидания,
не увеличение readiness timeout контроллера. Логи journald ограничены внутри VM.
На реальном hosted runner KVM оказался недоступен, выбран TCG. В CI cloud-init final
начался только на 288-й секунде: pinned image ожидает network-online около 120 с
даже без NIC, затем выполняет config/final. Поэтому первый boot имеет отдельный
8-минутный предел; сообщение о его превышении указывает boot и serial log. Общий
30-минутный бюджет, повторный boot и controller readiness 8/1 с не увеличены.

Workflow не использует repository secrets, environments, SSH или deployment. Токен
checkout имеет только `contents: read` и не сохраняется в checkout/seed. На forks
GitHub может потребовать стандартное одобрение запуска workflow владельцем.

## Один локальный запуск

На отдельном Windows/Linux компьютере с Python 3.12+, Git и QEMU:

```sh
python tests/systemd_harness/runner.py
```

Нужен чистый checkout коммита harness и Git-объекты 75d1654, 0a9a453, c77b5f7.
Runner сам найдёт QEMU в PATH или `C:\Program Files\qemu`, скачает image, создаст ISO,
подготовит VM, выполнит два boot и соберёт отчёт. `--image <file>` позволяет повторно
использовать локальный **base image с указанным SHA**, всегда read-only; это не
поддержка произвольного существующего VM-диска. `--accel tcg` доступен для хостов
без рабочего KVM. Никаких дополнительных команд внутри Ubuntu не требуется.

При ошибке сохраняются `summary.json`/логи. Автоматических повторов проваленных
сценариев и увеличения их времени нет. Неполный набор, неверный SHA, отсутствие
cleanup или неподтверждённый boot recovery не считаются PASS. Если среда не позволяет
запуск, результат BLOCKED/FAIL содержит причину: установить недостающий QEMU на
отдельном тестовом хосте либо разрешить этот CI job; guest/Oracle менять не требуется.

## Причина прежнего отказа updater

В systemd 255 job ID=0 печатается как пустое значение `Job`. Обычный `show` скрывает
пустые свойства: updater видел отсутствующий ключ (`None`) и отвергал inactive units.
Это следует из [реализации v255](https://github.com/systemd/systemd/blob/v255/src/systemctl/systemctl-show.c)
и [подавления пустых свойств](https://github.com/systemd/systemd/blob/v255/src/shared/bus-print-properties.c).
`0 loaded units listed` означает normal GC, не отсутствие unit-файлов.

Теперь query явно задаёт свойства и `--all`; требуется присутствующий `Job=`/`Job=0`,
ненулевые PID/job приводят к отказу. У timer PID-свойства могут отсутствовать, у service — обязательны.
Ошибки содержат имя unit, конкретное поле/значение, exit code или timeout. Неизвестный
ответ, active/activating, enabled boot units и реальные процессы по-прежнему запрещены.

## Резервная ручная процедура

[harness-update.md](harness-update.md) и [systemd-integration-harness.md](systemd-integration-harness.md)
сохраняются для расследования существующей ручной VM. Они больше не являются
основным способом прохождения сценариев. Новая автоматизация их данные не удаляет.

Unit-тесты runner проверяют протокол/контрольные суммы/state machine на имитациях.
Настоящий результат systemd подтверждается только конкретным `summary.json=PASS`
и его артефактами реального прогона; наличие workflow или успешные unit-тесты этого
не доказывают.
