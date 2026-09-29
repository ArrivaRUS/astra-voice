# Правка `.github/workflows/ci.yml` для AppImage — один раз, 05–06.10

> Для заказчика. Подготовлено агентом 29.09 в ветке `wip/r2-ci` (arch/appimage.md §9.4, T1 MN-9,
> T3 P2-2/P2-6). Токен агента не может менять `.github/workflows`, поэтому правку вносите вы
> в веб-редакторе GitHub. Полный готовый файл — `packaging/appimage/ci.yml.proposed`
> (проверен тем же гейтом `scripts/ci_lint.py`, что и настоящий workflow); ниже — разница с
> текущим `ci.yml`.

## Как внести

1. Откройте на GitHub `.github/workflows/ci.yml` в ветке `main` → карандаш «Edit».
2. Замените содержимое файла целиком текстом из `packaging/appimage/ci.yml.proposed` (кнопка
   «Raw» → выделить всё → скопировать).
3. «Commit changes» прямо в `main` с сообщением `ci: job appimage и выпуск через скрипты (§9.4)`.
4. Дождитесь зелёного CI на этом коммите (все job, включая новый «сборка AppImage и гейты»).

## Что сверить глазами (простым языком)

- **Новых секретов нет.** Ключ подписи по-прежнему виден только шагу «Отсоединённая подпись
  SHA256SUMS» в job `release`, и этот шаг не запускает скриптов репозитория, кроме прежней
  проверки ключа `check_signing_secret.py`.
- **Права не расширены.** Наверху файла, как и раньше, `contents: read`; право записи — только
  у job `release`; `environment: release` (ваше одобрение перед выпуском) на месте.
- **Сторонние действия — те же и по тем же отпечаткам**: `checkout`, `cache`, `upload-artifact`
  с теми же длинными SHA, что уже стоят в файле. Новых действий из интернета нет.
- **Везде `persist-credentials: false`** — после скачивания кода токен GitHub не остаётся на
  диске сборочной машины (находка ИБ T3 P2-2).
- **Новый job `appimage`** только вызывает три скрипта из репозитория: `build.sh --fetch`
  (скачать закреплённые по хэшу входы), `build.sh` (собрать и проверить), `smoke.sh`
  (проверить готовый файл). Если в репозитории нет файла `packaging/appimage/ENABLED`,
  скрипты ничего не делают и job зелёный.
- **Job `release`** вместо россыпи команд вызывает `scripts/release_build.sh` (собрать `.deb`,
  **поставить именно этот пакет** и проверить — находка ИБ T3 P2-6; собрать AppImage при
  `ENABLED`) и `scripts/release_assets.sh` (список файлов, суммы, `latest.json` — без ключа
  подписи). Публикует ровно список из `dist/assets.txt`.
- **Не запускать CI на правках документов:** добавлены `arch/**` и `spikes/**` — это
  архитектурные заметки и черновики, в пакеты они не попадают.

После этой правки менять workflow ради AppImage больше не нужно: снять AppImage с выпуска —
удалить `packaging/appimage/ENABLED`; поменять сборку — правка в `packaging/appimage/*`.

## Пункты §9.4 и где они в правке

| §9.4 | Что | В правке |
|---|---|---|
| п.1 | job `appimage`: `debian:12`, 30 мин, без `needs`, checkout без токена, apt, кэш по `hashFiles('packaging/appimage.lock')`, `build.sh --fetch` → `build.sh` → `smoke.sh`, выгрузка образа и SBOM | job `appimage`; к списку apt добавлены `curl ca-certificates gpgv util-linux python3-pip` (скачивание, проверка подписи runtime, запуск не от root, `pip download`) и хостовые библиотеки Qt по замеру спайка; выгрузка — только при `ENABLED` (`if: hashFiles(...)`), иначе `if-no-files-found: error` ронял бы выключенный job |
| п.2 | job `release`: `needs` + `appimage`, 35 мин, checkout без токена, apt из п.1, `scripts/release_build.sh`, `scripts/release_assets.sh dist` без `env`, подпись отдельно, `gh release create … $(cat dist/assets.txt)` | job `release` |
| п.3 | `paths-ignore`: `arch/**`, `spikes/**` | `on.push.paths-ignore`; белый список `scripts/ci_lint.py` расширен в этой же ветке |
| п.4 | больше ничего: `permissions`, пины SHA, `environment: release`, запрет `pull_request_target` | без изменений; `ci_lint.py` зелёный на `ci.yml.proposed` (тест `test_proposed_workflow_passes`) |
| п.5 | отдельный job подписи (T-121) | не входит, v1.0 |

Сверх §9.4: `persist-credentials: false` добавлен и в остальные job (`lint`, `unit`, `xvfb`,
`ci-lint`, `engine`, `deb`) — так требует T-131 (T3 P2-2: «во всех checkout»); на сборку не влияет.

## Находки ИБ, которые закрывает правка

- **T3 P2-2** — `actions/checkout` оставлял токен GitHub с правом записи в `.git/config`, и любой
  следующий шаг мог им воспользоваться. Теперь `persist-credentials: false` везде; публикация
  идёт явным `GH_TOKEN` только в шаге «Публикация». Суммы и `latest.json` собираются в шаге
  **без** секрета подписи (`release_assets.sh` сам проверяет, что секрета в окружении нет).
- **T3 P2-6** — раньше подписывался `.deb`, пересобранный в job `release`, а проверку установки
  проходил другой, из job `deb`. Теперь `release_build.sh` ставит в систему ровно подписываемый
  файл, запускает `astra-voice --version` и смоук и сверяет, что sha256 пакета не изменился.
- **T1 MN-9** — число ELF и предельная glibc для AppImage берутся из `packaging/appimage.lock`
  (`# expect-elf:`, `# max-glibc:`); новый ELF из обновлённого колеса роняет job `appimage`.

## Что должно быть готово в репозитории до правки (делает команда, не вы)

- В `packaging/appimage.lock` закреплены хэши `jsonschema`/`attrs`/`pyrsistent` (строки
  `# TODO-HASH` заменены требованиями с `--hash`) — иначе job `appimage` красный намеренно.
- Ключ подписи runtime AppImage `packaging/appimage/keys/appimage-runtime.gpg` (отпечаток
  `570C77ACEA40C0F1B758902CBF96CCA56490F695`, строка `# runtime-key:` в lock) — без него
  `build.sh --fetch` останавливается.
- Первый прогон нового job на ветке — зелёный (агенты проверяют до отправки вам).

## Разница с текущим `ci.yml`

```diff
--- .github/workflows/ci.yml
+++ .github/workflows/ci.yml
@@ -21,6 +21,9 @@
       - 'docs/status.md'
       - 'docs/plans.md'
       - 'docs/test-plan.md'
+      # Архитектура и спайки не входят ни в .deb, ни в AppImage (arch/appimage.md §9.4 п.3).
+      - 'arch/**'
+      - 'spikes/**'
   pull_request:
     branches: [main]
 
@@ -44,6 +47,8 @@
     container: debian:12
     steps:
       - uses: actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09 # v5.0.0
+        with:
+          persist-credentials: false
       - name: Системные пакеты
         run: |
           apt-get update
@@ -62,6 +67,8 @@
     container: debian:12
     steps:
       - uses: actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09 # v5.0.0
+        with:
+          persist-credentials: false
       - name: Системные пакеты
         # test_real_qml_* грузит настоящий qml/Pill.qml; нужны QML-модули.
         # Трей рендерит SVG плагином Qt из libqt5svg5.
@@ -100,6 +107,8 @@
     container: debian:12
     steps:
       - uses: actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09 # v5.0.0
+        with:
+          persist-credentials: false
       - name: Системные пакеты (Qt + виртуальный дисплей)
         # Трей рендерит SVG плагином Qt; libqt5svg5 нужен явно.
         run: |
@@ -147,6 +156,8 @@
     container: debian:12
     steps:
       - uses: actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09 # v5.0.0
+        with:
+          persist-credentials: false
       - run: |
           apt-get update
           apt-get install -y --no-install-recommends python3 python3-yaml
@@ -160,6 +171,8 @@
     container: debian:12
     steps:
       - uses: actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09 # v5.0.0
+        with:
+          persist-credentials: false
       - name: Системные пакеты
         run: |
           apt-get update
@@ -236,6 +249,8 @@
     container: debian:12
     steps:
       - uses: actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09 # v5.0.0
+        with:
+          persist-credentials: false
       - name: Инструменты сборки
         run: |
           apt-get update
@@ -273,12 +288,54 @@
             dist/sbom.cdx.json
           if-no-files-found: error
 
+  appimage:
+    name: сборка AppImage и гейты
+    # Второй артефакт v0.2 (arch/appimage.md §9). Workflow только вызывает скрипты
+    # репозитория; без packaging/appimage/ENABLED build.sh и smoke.sh завершаются
+    # успешно с пометкой «AppImage выключен», артефакт не выгружается.
+    runs-on: ubuntu-latest
+    timeout-minutes: 30
+    container: debian:12
+    steps:
+      - uses: actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09 # v5.0.0
+        with:
+          persist-credentials: false
+      - name: Системные пакеты (сборка, подпись runtime, Qt без дисплея и под xvfb)
+        run: |
+          apt-get update
+          apt-get install -y --no-install-recommends \
+            binutils file patch git curl ca-certificates gpgv util-linux \
+            python3 python3-pip \
+            xvfb xauth fontconfig libfontconfig1 libglib2.0-0 libgl1 libdbus-1-3 \
+            libgssapi-krb5-2 libx11-6 libx11-xcb1 libxkbcommon0 libxkbcommon-x11-0 \
+            libxcb-glx0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 \
+            libxcb-render0 libxcb-render-util0 libxcb-shape0 libxcb-shm0 libxcb-sync1 \
+            libxcb-util1 libxcb-xfixes0 libxcb-xinerama0 libxcb-xkb1
+      - uses: actions/cache@caa296126883cff596d87d8935842f9db880ef25 # v5.0.1
+        with:
+          path: ~/.cache/astra-voice-dev/appimage
+          key: appimage-${{ hashFiles('packaging/appimage.lock') }}
+      - name: Входы по lock (единственный шаг с сетью)
+        run: packaging/appimage/build.sh --fetch
+      - name: Сборка и гейты
+        run: packaging/appimage/build.sh
+      - name: Смоук образа
+        run: packaging/appimage/smoke.sh dist/Astra_Voice-*-x86_64.AppImage
+      - uses: actions/upload-artifact@330a01c490aca151604b8cf639adc76d48f6c5d4 # v5.0.0
+        if: hashFiles('packaging/appimage/ENABLED') != ''
+        with:
+          name: astra-voice-appimage
+          path: |
+            dist/Astra_Voice-*.AppImage
+            dist/sbom-appimage.cdx.json
+          if-no-files-found: error
+
   release:
     name: релиз по тегу
     if: startsWith(github.ref, 'refs/tags/v')
-    needs: [lint, unit, xvfb, engine, ci-lint, deb]
+    needs: [lint, unit, xvfb, engine, ci-lint, deb, appimage]
     runs-on: ubuntu-latest
-    timeout-minutes: 20
+    timeout-minutes: 35
     container: debian:12
     # Секрет подписи живёт только здесь: Environment с required reviewer.
     environment: release
@@ -292,6 +349,7 @@
       - uses: actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09 # v5.0.0
         with:
           fetch-depth: 0
+          persist-credentials: false
       - name: Версия тега и changelog
         run: |
           version="${GITHUB_REF_NAME#v}"
@@ -306,9 +364,14 @@
           # Для гейта смоука установленного дерева: модули UI импортируют PyQt5 на уровне модуля.
           apt-get install -y --no-install-recommends \
             build-essential debhelper dh-python devscripts dpkg-dev fakeroot \
-            lintian file binutils git make gnupg gpgv curl jq \
+            lintian file binutils git make gnupg gpgv curl jq patch ca-certificates util-linux \
             python3 python3-venv python3-pip python3-pyqt5 python3-pyqt5.qtquick \
-            python3-requests python3-jsonschema
+            python3-requests python3-jsonschema \
+            xvfb xauth fontconfig libfontconfig1 libglib2.0-0 libgl1 libdbus-1-3 \
+            libgssapi-krb5-2 libx11-6 libx11-xcb1 libxkbcommon0 libxkbcommon-x11-0 \
+            libxcb-glx0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 \
+            libxcb-render0 libxcb-render-util0 libxcb-shape0 libxcb-shm0 libxcb-sync1 \
+            libxcb-util1 libxcb-xfixes0 libxcb-xinerama0 libxcb-xkb1
       - name: Тег стоит на коммите из main
         run: |
           test -d .git || { echo "::error::Checkout без истории Git"; exit 1; }
@@ -316,21 +379,18 @@
           git merge-base --is-ancestor "$GITHUB_SHA" origin/main || { echo "::error::Тег не на коммите из main"; exit 1; }
       - name: Связка ключей в белом списке
         run: python3 scripts/check_keyring.py data/keys/release.gpg
-      - name: Колёса
-        run: packaging/build-deb.sh --fetch-wheels
       # Сборка и подпись — в одном job: между ними артефакт никуда не уезжает (У32).
-      - name: Сборка и гейты
-        run: packaging/build-deb.sh --dh
-      - name: SHA256SUMS и отсоединённая подпись
+      # .deb, установка именно подписываемого пакета (T3 P2-6), AppImage при ENABLED.
+      - name: Сборка, установка и гейты
+        run: scripts/release_build.sh
+      # Без секрета (T3 P2-2): latest.json, SHA256SUMS, dist/assets.txt.
+      - name: Ассеты выпуска
+        run: scripts/release_assets.sh dist
+      - name: Отсоединённая подпись SHA256SUMS
         env:
           GPG_SIGNING_KEY: ${{ secrets.GPG_SIGNING_KEY }}
         run: |
           cd dist
-          test -f ../docs/INSTALL-ADMIN.md || { echo '::error::Нет docs/INSTALL-ADMIN.md'; exit 1; }
-          cp ../docs/INSTALL-ADMIN.md .
-          cp ../data/keys/release.gpg release.gpg
-          python3 ../scripts/release_latest_json.py --dist . --version "${GITHUB_REF_NAME#v}"
-          sha256sum astra-voice_*.deb sbom.cdx.json INSTALL-ADMIN.md latest.json release.gpg > SHA256SUMS
           export GNUPGHOME="$(mktemp -d)"
           chmod 700 "$GNUPGHOME"
           printf '%s' "$GPG_SIGNING_KEY" | gpg --batch --quiet --import
@@ -348,6 +408,6 @@
           echo "deb [signed-by=/usr/share/keyrings/githubcli.gpg] https://cli.github.com/packages stable main" \
             > /etc/apt/sources.list.d/github-cli.list
           apt-get update && apt-get install -y --no-install-recommends gh
-          gh release create "${GITHUB_REF_NAME}" --generate-notes \
-            dist/astra-voice_*.deb dist/sbom.cdx.json dist/SHA256SUMS dist/SHA256SUMS.asc \
-            dist/INSTALL-ADMIN.md dist/latest.json dist/release.gpg
+          # Список ассетов пишет scripts/release_assets.sh (в т. ч. AppImage при ENABLED).
+          # shellcheck disable=SC2046
+          gh release create "${GITHUB_REF_NAME}" --generate-notes $(cat dist/assets.txt)
```
