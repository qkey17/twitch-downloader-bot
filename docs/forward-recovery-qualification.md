# Ограниченная квалификация forward recovery deployment

29 октября 2026. База разработки: main `1b01b793051b85355a2eea2eb48aa91323379a28`.
Это результаты изолированного стенда, **не Oracle production** и не разрешение на deployment.

## Проверенные исходники и окружение

- SHA256 `deploy/oracle_deploy.sh`:
  `8d97a1d46888137d07c138d41a7ab0d0f060f13114200694548f350d53610239`.
- Проверенная Ubuntu cloud image SHA256:
  `0533b0655c32e68b31d792ecd6ccfca95abdbc536c4446874fe0513bd4140ffe`.
- Ubuntu 24.04.4 LTS, Python 3.12.3, systemd 255.4-1ubuntu8.16, Git 2.43.0.
- QEMU/TCG: новый overlay, 954 MiB RAM, 1 CPU, `-nic none`, без host mounts,
  DMI `TDB-Harness/TDB-Isolated-VM`; ограничение всего прогона 1200 секунд.
- Проверка PID 1, namespace, virtio root, отсутствия сети/production paths
  выполнена до тестовых изменений. Исходники переданы через read-only seed,
  SHA256 проверены в госте; отчёт извлечён проверяемыми chunked serial frames.

Lab bindings перечислены в `tests/deployment_recovery_lab.py` и в JSON отчёта:
заменены только фиксированные пути checkout/state/helper/socket, имя отдельного
тестового unit и разрешённый URL origin на локальный Git remote.
Функции helper и его тайм-ауты не заменялись; пользователь — настоящий `ubuntu`,
sudo/ready/journal/systemd настоящие. SHA256 helper после этих явных bindings:
`c6fc72d7465c54f2a7e1d9ae34b6c24d53036e0871308b59e921ff3ff70b8752`.

## Результаты

- Полный Windows suite в существующей проектной venv: **513 tests, OK, 29 skips**.
  Пропуски относятся к POSIX/Windows возможностям, включая 13 новых recovery tests.
- Linux/Python 3.12: **29 целевых tests, OK, без skips**. Настоящий временный Git
  working → broken → fixed; ложные systemd-ответы применяются только в offline
  unit-тестах, а не в следующей интеграционной проверке.
- Compileall `src tools tests`, Bash syntax helper/workflow и `git diff --check`: OK.
- Обычный check/deploy сохранил контракт; первоначально неработающая служба
  по-прежнему не допускается обычным preflight.

| Настоящее исходное состояние | После emergency recovery | Итог |
|---|---|---|
| failed/failed, MainPID=0, Job пуст | active/running, PID 4099, InvocationID `2623974d70f8400093b2352b53187555`, NRestarts=0 | PASS |
| inactive/dead, MainPID=0, Job пуст | active/running, PID 4302, InvocationID `595b6947a9734a32a5ae19cde12fda8d`, NRestarts=0 | PASS |

Установленные синтетические commits:
`82dec118cb1768b24a706f294da8010bde24d5c1` (broken) →
`986882c1b6da744960c1432085cc8521b8dfe4d5` (fixed) →
`eb8fccc36a793c09741a497f7ef6bb3c7c056bad` (следующий forward commit).
Это локальная тестовая история, не commits репозитория проекта.

Для обоих случаев подтверждены точный SHA, `DEPLOYED TARGET`, текущая process
identity, readiness marker и полный production stability interval 10 секунд.
Хеши двух data fixtures сохранены. Fixtures — непрозрачные синтетические файлы,
а не проверка целостности настоящей SQLite или backup/restore.
Cleanup: тестовая служба inactive/dead, MainPID=0, Job пуст; тестовые unit/sudoers
удалены из VM, VM штатно выключена. Исходный image не изменялся.

## Ограничения и сохранённая диагностика

Первые два гостя остановились на раннем `IO-APIC + timer` kernel panic, до
исполнения тестов. Включение `-rtc ...,clock=vm` из ранее рабочего стенда позволило
загрузку. Следующий seed был отвергнут checksum guard из-за CRLF/LF; исправлен
перенос байтов через base64. Guard не отключался, критерии PASS не менялись.
Эти незавершённые подготовки не объявляются успешными квалификациями.

Успешный ограниченный прогон сохранён локально в `_work/forward-recovery-systemd-5`:
`launch.json`, seed, overlay, raw serial, `result.json`, `unit-tests.log`, journal.
SHA256 raw serial: `02547cdeb100c1e004bc75bcb1f5bee21c2c187e197c92a0ed16485726c28333`.
SHA256 result JSON: `249f7f39fbdb9fbcb0c8a9b53b45aedc2bff57b11550fa6e232946a325822de7`.
Артефакты VM не добавлены в Git.

Синтетический venv заменяет только `pip check` заглушкой; compile/import,
служба, sudo и journal/readiness настоящие. Полный Windows suite проверялся
с действительными установленными Python dependencies. Первоначальная попытка
полного suite глобальным Python была непригодна из-за отсутствующих dependencies;
пакеты не устанавливались, финальный suite выполнен проектной venv.

В тестовой службе `Restart=no`: взаимодействие с production `Restart=on-failure`
и автоматическими рестартами этим интеграционным прогоном не квалифицировано.
Helper не меняет политику systemd и не повторяет start из аварийного error trap;
оператор обязан наблюдать существующую restart policy при неудачной версии.

Не испытывались Telegram/Twitch, реальные secrets/данные, Oracle, Dropbox,
полный daily backup suite в QEMU или production bootstrap. До первого deployment
нужны отдельное разрешение, установка и проверка root-owned helper по
[эксплуатационной инструкции](oracle-deployment.md#bootstrap-helper-должен-быть-установлен-заранее).
