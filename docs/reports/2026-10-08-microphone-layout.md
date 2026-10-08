# Первое открытие: строка микрофона

## Дополнение 08:38 МСК: первоначальное чтение состояния

Первый фикс геометрии применён к демоверсии с разрешения пользователя. Новый
снимок выявил второй дефект: SettingsBridge подключается после Component.onCompleted,
поэтому refreshDevices/refreshMicrophone вообще не вызываются. Read-only pactl
подтвердил системные 100%, mute=no. Прежний тест предварительно заполнял состояние
и скрывал дефект; это предварительное чтение удалено.

Добавлен локальный refreshSettings по готовности компонента/изменению моста и
identity guard. Ранний/поздний мост читается ровно один раз, новый экземпляр —
ещё один раз, повтор того же объекта — без чтения. Геометрия не менялась.
Независимый RED: 4 FAIL/2 PASS, финальный GREEN: 34 PASS за 9.27 с, без QML warnings.
Старые 14 тестов минимального окна PASS; независимое code review без замечаний.
General.qml SHA256 `d80d36825bacecbc64b978af28fa11a2d5f9c0c118ed0905d6878122c4128912`.
Delta QML SHA256 `41902e142fd18632afb48c62458c349e5de921e839f0ec5f7c415bef95491a9e`.
Test SHA256 `e01e072fe5686b70d590632a8425bc4da45a7dc9bf7e102f846b3d8e9613a89f`.
Новый CI ещё ожидается; ниже — история первого фикса.

Владелец прислал дефект демоверсии `0.1.1~dev6+command.demo.20261008.git12f632c`:
подпись микрофона и помощь съехали под выбор устройства. Системный пакет по dpkg
остаётся dev5; демоверсия запущена отдельно из распакованного пакета Cowork.
Эта задача не изменяла ни одну из них, профиль, звук или микрофон.

## Причина и исправление

В реальном запуске settingsBridge появляется после engine.load, до первого show.
Две вложенные Layout строки микрофона меняют видимость, а SettingRow запрашивает
их implicitHeight во время пересчёта. Qt5 обнаруживает цикл и оставляет подпись
шириной0. Реальный журнал демоверсии в07:50:11 МСК содержит соответствующий
`General.qml:237: QML SettingRow: Binding loop detected for property "implicitHeight"`.
Раннее подключение подставного моста в прежних снимочных тестах скрывало дефект.

Изменён только General.qml: естественный размер локального контейнера и две Row,
локальная высота строки. SettingRow.qml, мост, системный звук и действия кнопок
не меняются. Компоновка B сохранена. Это исправление геометрии: сообщение
неизвестной громкости само по себе не свидетельствует о поломке записи и не
устраняется этим дифом.

## Доказательства

База722b75aa9b4694ca6e4b09b34034ca2b8b8fa00e.
QML diff SHA256 `fb1a7defd42c68cb65aaa48a4133fae0b5ebcc5336c50a39e226ab6303d3d239`.
General.qml SHA256 `14a1941c7949aeb5afc72ce27e23acbbf12e91818a6a837f440adce496cbd90c`.
Независимый тест SHA256 `55a12ae3663466f6a923eddefd262bd953b626af56ecaa549c1cc8bea71d18d8`.

- База:12 late-bridge FAIL (label.width=0),4 перехода capability FAIL (Binding loop),
  ранняя статическая матрица12 PASS.
- Исправление:28 независимых PASS за7.76с, предупреждений QML нет. Состояния
  unknown/muted/normal и locked-варианты, ширины900/1035, первый показ,
  переходы состояний и hide/resize/show. Настоящий SettingsBridge,
  подставлен только SettingsApply без системных вызовов.
- Существующие14 проверок минимальной высоты окна PASS за12.52с.
- Штатный make lint PASS: Ruff/format, mypy250 файлов; git diff --check PASS.
- Независимый code reviewer сверил точный диф и before/after PNG1035/900:
  открытых замечаний нет. Автор не принимал собственный код.

Точные before/after PNG и JSON геометрии сохранены в gitignored
`dist/microphone-layout-check-2026-10-08/`. На1035px ширина подписи0→336,
selector x262→598; на900px подпись0→212, selector x256→468.

Команда независимого GREEN (VENV=/home/astra/.cache/astra-voice-dev/venv-a):

```sh
env -u DISPLAY -u WAYLAND_DISPLAY -u QT_ACCESSIBILITY -u AT_SPI_BUS_ADDRESS \
QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software \
DBUS_SESSION_BUS_ADDRESS=unix:path=/nonexistent \
DBUS_SYSTEM_BUS_ADDRESS=unix:path=/nonexistent \
PULSE_SERVER=unix:/nonexistent ASTRA_VOICE_REQUIRE_QT=1 \
/home/astra/.cache/astra-voice-dev/venv-a/bin/python -m pytest \
tests/xvfb/test_microphone_layout_regression.py \
-q -o addopts='' --tb=short -p no:cacheprovider
```

CI новой ветки ещё не завершён. Живые KDE/Fly, установленная демосборка,
микрофон и звук этой проверкой не приняты. Дерево Cowork не изменялось.

## Параллельная задача AppImage

PR44 commit0c210c920e204ec575bcd06ce0cabc1a056ca51c получил все7 обязательных
CI PASS, run37730627263, release skipped. Unit8081 PASS/14 SKIP,
xvfb465 PASS/2 SKIP. Настоящий smoke проверил самоустановку, IPC/show, удаление
неактивной и активной копии, сохранность настроек/моделей/журналов/чужих файлов.
AppImage75065848байт, BUILD_ID aa92587ad9ca, SHA256
`7c3567616681fe320feed8ef7ffbef0d827b3d5052e6374bcd96b45cb2277247`.
Релиза и установки не было; code/security review завершены независимо.
