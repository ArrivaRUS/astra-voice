# Предложение CI для AppImage

Актуализировано относительно `.github/workflows/ci.yml` из коммита
`5c0e56e9efd95334d972aea3752fd65b348da430` (2026-10-07).
Полный файл для применения: [ci.yml.proposed](ci.yml.proposed).
Действующий workflow этим пакетом не изменяется. По принятому порядку владелец
применяет подготовленный файл в браузере: у токена команды нет права Workflows.
Пакет сначала проходит независимое ревью и проверку тестировщиком; применение
и выпуск координируются отдельно.

## Что изменится

- Добавится job `appimage` в `debian:12`, с лимитом 30 минут. Git устанавливается
  до checkout; входы кэшируются по `packaging/appimage.lock`. Затем выполняются
  `build.sh --fetch`, `build.sh` и `smoke.sh`; образ и SBOM выгружаются как артефакт.
  Без `packaging/appimage/ENABLED` скрипты пропускают сборку и смоук, выгрузка отключена.
- Job `release` дождётся также `appimage`. `scripts/release_build.sh` заново
  собирает оба артефакта, устанавливает и проверяет именно выпускаемый `.deb`,
  проверяет AppImage и создаёт архив исходников. `scripts/release_assets.sh dist`
  без секрета формирует `latest.json`, `SHA256SUMS` и `assets.txt`.
- Секрет остаётся только в inline-шаге подписи внутри Environment `release`.
  Существующие проверки ключа и полученной подписи сохранены. Временная связка
  ключей удаляется через `EXIT` trap также при отказе; после импорта переменная
  секрета снимается. Репозиторные скрипты сборки и подготовки ассетов секрета не получают.
- `gh` устанавливается обычным `apt-get install` из репозиториев контейнера
  Debian bookworm на этапе инструментов, до подписи. Внешний apt-репозиторий
  GitHub CLI не добавляется. `GH_TOKEN` передаётся только шагу публикации;
  он публикует список `dist/assets.txt` и ничего не устанавливает (П25/MN-19).
- Каждый checkout получает `persist-credentials: false`. Глобальное
  `contents: read`, единственное `contents: write` у `release`, Environment,
  tag gate, проверка принадлежности тега `main` и пины Actions сохраняются.

Все прежние jobs, их команды, таймауты, окружение, фильтры запуска, шрифтовый гейт
и проверки движка сохранены; в шести jobs вне выпуска меняется только сохранение
credentials после checkout. Старые дополнительные правки `paths-ignore` и unit
import gate сюда не переносятся. Лимит job `release` остаётся текущим — 20 минут.
Достаточность этого лимита с полной сборкой AppImage ещё не измерена в GitHub CI.
Локальное послабление для незакреплённых хэшей в workflow не включено.

## Как применить

1. После ревью выбрать согласованный коммит с этим пакетом. Перед заменой
   сравнить его `.github/workflows/ci.yml` с текущим файлом целевой ветки.
   SHA256 исходного файла для этого предложения:
   `fa521606fba1aed81bcd896b3002174a2f73e09100ba7eecfc375d86c238f56d`.
   Если workflow изменился, сначала обновить предложение, сохранив новые правки.
2. Открыть `packaging/appimage/ci.yml.proposed` **из выбранного коммита** через
   Raw и скопировать весь файл. Открыть `.github/workflows/ci.yml` целевой ветки
   в редакторе GitHub и заменить содержимое.
3. Сохранить правку в отдельной ветке и открыть PR в `main`. Сверить diff с
   приведённым ниже и дождаться семи проверок: `lint`, `unit`, `xvfb`, `ci-lint`,
   `engine`, `deb`, `appimage`. Job `release` на обычном PR пропускается по tag gate.
4. После зелёного CI и ревью согласовать слияние с координатором. Для исправления
   красного job передать ссылку на прогон; повторные изменения workflow могут
   потребоваться, если проблема окажется в его командах или окружении.

Тег ради проверки этого пакета создавать не нужно. Проверка job `release`
потребует отдельно разрешённого выпуска и одобрения Environment. Этот пакет
не снимает оставшиеся условия выпуска AppImage, включая вопрос исходников GCC;
запрет их скачивания сохраняется.

## Проверки и ограничения

Офлайн-команды для подготовленного файла:

```sh
python3 scripts/ci_lint.py .github/workflows/ci.yml packaging/appimage/ci.yml.proposed
python3 -m pytest tests/unit/test_appimage_workflow_proposal.py tests/unit/test_ci_lint.py -q
python3 -m pytest tests/unit/test_appimage_packaging.py -k 'proposed or patch_doc' -q
```

Новый тест сопоставляет существующие jobs и настройки с действующим CI,
проверяет область секретов, установку `gh` до публикации, точность diff ниже
и синтаксис всех `run` через `bash -n` без исполнения команд. Существующие тесты
проверяют пины, permissions, tag gate, checkout без credentials и разделение
сборки/ассетов/подписи. Команды pytest выполняются в проектном venv с изоляцией
Qt/D-Bus, принятой в регламенте команды.

Эти проверки не запускают GitHub Actions, сеть, установку пакетов, GUI,
сборку образа или публикацию. Доступность пакетов в контейнере, фактическое время
сборки и первый прогон нового job остаются непроверенными. Исторические результаты
сборки не считаются результатами этого предложения.

## Точный diff относительно действующего workflow

```diff
--- .github/workflows/ci.yml
+++ .github/workflows/ci.yml
@@ -44,6 +44,8 @@
     container: debian:12
     steps:
       - uses: actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09 # v5.0.0
+        with:
+          persist-credentials: false
       - name: Системные пакеты
         run: |
           apt-get update
@@ -62,6 +64,8 @@
     container: debian:12
     steps:
       - uses: actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09 # v5.0.0
+        with:
+          persist-credentials: false
       - name: Системные пакеты
         # test_real_qml_* грузит настоящий qml/Pill.qml; нужны QML-модули.
         # Трей рендерит SVG плагином Qt из libqt5svg5.
@@ -100,6 +104,8 @@
     container: debian:12
     steps:
       - uses: actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09 # v5.0.0
+        with:
+          persist-credentials: false
       - name: Системные пакеты (Qt + виртуальный дисплей)
         # Трей рендерит SVG плагином Qt; libqt5svg5 нужен явно.
         run: |
@@ -147,6 +153,8 @@
     container: debian:12
     steps:
       - uses: actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09 # v5.0.0
+        with:
+          persist-credentials: false
       - run: |
           apt-get update
           apt-get install -y --no-install-recommends python3 python3-yaml
@@ -160,6 +168,8 @@
     container: debian:12
     steps:
       - uses: actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09 # v5.0.0
+        with:
+          persist-credentials: false
       - name: Системные пакеты
         run: |
           apt-get update
@@ -236,6 +246,8 @@
     container: debian:12
     steps:
       - uses: actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09 # v5.0.0
+        with:
+          persist-credentials: false
       - name: Инструменты сборки
         run: |
           apt-get update
@@ -273,10 +285,58 @@
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
+      # build.sh берёт в образ только файлы под Git: без git в контейнере checkout
+      # скачал бы архив без .git.
+      - name: Git для checkout с историей файлов
+        run: |
+          apt-get update
+          apt-get install -y --no-install-recommends git ca-certificates
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
     timeout-minutes: 20
     container: debian:12
@@ -292,6 +352,7 @@
       - uses: actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09 # v5.0.0
         with:
           fetch-depth: 0
+          persist-credentials: false
       - name: Версия тега и changelog
         run: |
           version="${GITHUB_REF_NAME#v}"
@@ -306,9 +367,14 @@
           # Для гейта смоука установленного дерева: модули UI импортируют PyQt5 на уровне модуля.
           apt-get install -y --no-install-recommends \
             build-essential debhelper dh-python devscripts dpkg-dev fakeroot \
-            lintian file binutils git make gnupg gpgv curl jq \
+            lintian file binutils git make gnupg gpgv curl jq patch ca-certificates util-linux gh \
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
@@ -316,38 +382,33 @@
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
+        shell: bash
         env:
           GPG_SIGNING_KEY: ${{ secrets.GPG_SIGNING_KEY }}
         run: |
           cd dist
-          test -f ../docs/INSTALL-ADMIN.md || { echo '::error::Нет docs/INSTALL-ADMIN.md'; exit 1; }
-          cp ../docs/INSTALL-ADMIN.md .
-          cp ../data/keys/release.gpg release.gpg
-          python3 ../scripts/release_latest_json.py --dist . --version "${GITHUB_REF_NAME#v}"
-          sha256sum astra-voice_*.deb sbom.cdx.json INSTALL-ADMIN.md latest.json release.gpg > SHA256SUMS
-          export GNUPGHOME="$(mktemp -d)"
+          GNUPGHOME=$(mktemp -d)
+          export GNUPGHOME
+          trap 'gpgconf --kill gpg-agent || :; rm -rf -- "$GNUPGHOME"' EXIT
           chmod 700 "$GNUPGHOME"
           printf '%s' "$GPG_SIGNING_KEY" | gpg --batch --quiet --import
+          unset GPG_SIGNING_KEY
           SIGNING_FPR="$(python3 ../scripts/check_signing_secret.py secret --homedir "$GNUPGHOME" --keyring ../data/keys/release.gpg)" || { echo '::error::Секрет подписи не прошёл проверку состава'; exit 1; }
           gpg --batch --yes --local-user "${SIGNING_FPR}!" --detach-sign --armor --output SHA256SUMS.asc SHA256SUMS
           gpgv --keyring ../data/keys/release.gpg SHA256SUMS.asc SHA256SUMS
           python3 ../scripts/check_signing_secret.py signature --keyring ../data/keys/release.gpg --subkey "$SIGNING_FPR" SHA256SUMS.asc SHA256SUMS
-          rm -rf "$GNUPGHOME"
       - name: Публикация
         env:
           GH_TOKEN: ${{ github.token }}
         run: |
-          curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
-            -o /usr/share/keyrings/githubcli.gpg
-          echo "deb [signed-by=/usr/share/keyrings/githubcli.gpg] https://cli.github.com/packages stable main" \
-            > /etc/apt/sources.list.d/github-cli.list
-          apt-get update && apt-get install -y --no-install-recommends gh
-          gh release create "${GITHUB_REF_NAME}" --generate-notes \
-            dist/astra-voice_*.deb dist/sbom.cdx.json dist/SHA256SUMS dist/SHA256SUMS.asc \
-            dist/INSTALL-ADMIN.md dist/latest.json dist/release.gpg
+          # Список ассетов пишет scripts/release_assets.sh (в т. ч. AppImage при ENABLED).
+          # shellcheck disable=SC2046
+          gh release create "${GITHUB_REF_NAME}" --generate-notes $(cat dist/assets.txt)
```
