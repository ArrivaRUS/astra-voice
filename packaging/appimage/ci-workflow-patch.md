# Предложение CI для AppImage

Актуализировано 11.11.2026 относительно текущего файла `.github/workflows/ci.yml`
в рабочем дереве catalog-sign. Git-коммит в этой работе не проверялся.
Полный справочный снимок: [ci.yml.proposed](ci.yml.proposed).
SHA256 действующего исходного workflow: `74240b8f84285628a37e87342bd57f3843a8fd3b5733afdacf2ab3b4a0ce2eaa`.

## Текущее состояние

Сборка AppImage уже включена в действующий CI. Предложение синхронизировано
с ним: YAML заданий, триггеров, прав и команд совпадает. Отличие точных байтов —
только поясняющий комментарий снимка в первой строке. Применение этого снимка
не добавляет новое задание и не меняет поведение CI.

Сохранены все актуальные правила:

- Каждый checkout имеет `persist-credentials: false`; права, SHA-пины Actions,
  фильтры запуска и зависимости заданий остаются такими же, как в ci.yml.
- Задание `appimage` вызывает репозиторные build/smoke-скрипты, использует lock
  и публикует артефакт только при ENABLED. Release зависит от appimage.
- Сборка, формирование ассетов и гейт каталога предшествуют подписи выпуска;
  GPG_SIGNING_KEY передаётся только inline-шагу импорта/подписи, GH_TOKEN — публикации.
- Гейт T-06 получает каталог `.github/workflows` и проверяет оба расширения
  `.yml`/`.yaml`. Комментарии о release учитывают отдельный catalog-sign.yml;
  фактическая граница окружения и кодовая политика описаны в docs/SECURITY.md.

## Как использовать снимок

Перед возможной заменой файла сравнить текущий ci.yml с указанной выше SHA256.
Если он изменился, сначала обновить снимок и точный diff, сохранив новые правки.
Обычная правка workflow проходит PR, CI и ревью владельца; выпуск и одобрение
Environment выполняются отдельно. Сейчас функциональной правки для применения нет.
Этот документ не разрешает сборку, публикацию или изменение настроек GitHub.

## Офлайн-проверки

```sh
python3 scripts/ci_lint.py .github/workflows/ci.yml packaging/appimage/ci.yml.proposed
python3 -m pytest -p no:cacheprovider -q -o addopts= tests/unit/test_appimage_workflow_proposal.py tests/unit/test_ci_lint.py
python3 -m pytest -p no:cacheprovider -q -o addopts= tests/unit/test_appimage_packaging.py -k 'proposed or patch_doc'
```

Pytest запускается в проектном venv с принятой изоляцией Qt/D-Bus. Тесты проверяют
сохранность настроек CI, области секретов, пины/права, точный diff ниже и синтаксис
run через bash -n без исполнения команд. Эти проверки не запускают GitHub Actions,
сборку образа, установку пакетов или публикацию.

## Точный diff относительно действующего workflow

```diff
--- .github/workflows/ci.yml
+++ .github/workflows/ci.yml
@@ -1,3 +1,4 @@
+# Справочный снимок действующего CI для AppImage; функциональных отличий нет.
 # CI Astra Voice. Инварианты этого файла проверяет scripts/ci_lint.py (гейт T-06):
 # все Actions — по SHA, permissions минимальные, секрет подписи только в
 # Environment `release`: здесь по тегу v*, в catalog-sign.yml — sign с main
```
