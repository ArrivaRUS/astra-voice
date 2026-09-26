#!/usr/bin/env bash
set -euo pipefail

usage() { echo 'Использование: scripts/release.sh vX.Y.Z [--push] [--skip-ci-check] [--dry-run]' >&2; }
if (($# < 1)) || [[ ! $1 =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo 'ОШИБКА: нужна версия вида vX.Y.Z.' >&2
    usage
    exit 2
fi
tag=$1
version=${tag#v}
shift
push=false
dry_run=false
skip_ci=false
for option in "$@"; do
    case $option in
        --push) push=true ;;
        --dry-run) dry_run=true ;;
        --skip-ci-check) skip_ci=true ;;
        *) echo "ОШИБКА: неизвестный аргумент $option" >&2; usage; exit 2 ;;
    esac
done
if $push && $dry_run; then
    echo 'ОШИБКА: --push и --dry-run несовместимы.' >&2
    usage
    exit 2
fi

root=$(git rev-parse --show-toplevel 2>/dev/null) || {
    echo 'ОШИБКА: запускать нужно из Git-репозитория.' >&2
    exit 1
}
cd "$root"
failed=0
check() {
    local label=$1 result=$2
    if [[ $result == true ]]; then
        echo "OK: $label"
    else
        echo "ОШИБКА: $label"
        failed=1
    fi
}

if status=$(git status --porcelain); then
    if [[ -z $status ]]; then check 'рабочее дерево чистое' true; else check 'рабочее дерево не чистое' false; fi
else
    check 'не удалось проверить состояние рабочего дерева' false
fi
branch=$(git branch --show-current)
if [[ $branch == main ]]; then check 'текущая ветка main' true; else check "текущая ветка $branch, требуется main" false; fi
for asset in docs/INSTALL-ADMIN.md data/keys/release.gpg; do
    if [[ -f $asset ]] && git ls-files --error-unmatch -- "$asset" >/dev/null 2>&1; then
        check "$asset существует и отслеживается Git" true
    else
        check "$asset отсутствует или не отслеживается Git" false
    fi
done
if python3 scripts/check_keyring.py data/keys/release.gpg >/dev/null; then
    check 'связка ключей в белом списке' true
else
    check 'связка ключей в белом списке' false
fi
head=$(git rev-parse HEAD)
if $push; then
    if git fetch origin main; then check 'git fetch origin main завершён' true; else check 'git fetch origin main не удался' false; fi
else
    echo 'Напоминание: сначала git fetch origin (используется локальная ссылка origin/main).'
fi
origin=$(git rev-parse --verify refs/remotes/origin/main 2>/dev/null || true)
if [[ -n $origin && $head == "$origin" ]]; then
    check 'HEAD совпадает с локальным origin/main' true
else
    check 'HEAD не совпадает с локальным origin/main' false
fi

changelog_version=$(sed -n '1s/.*(\([^)]*\)).*/\1/p' packaging/debian/changelog 2>/dev/null || true)
if [[ $changelog_version == "$version" ]]; then
    check "версия первой записи changelog: $version" true
else
    check "первая запись changelog должна иметь версию $version" false
    echo "Подсказка: добавьте запись через dch -v $version -D unstable или вручную; версия пакета берётся из changelog."
fi
if [[ -f src/astra_voice/_version.py ]]; then
    generated=$(sed -n 's/^__version__ = ["'"']\([^"'"']*\)["'"']/\1/p' src/astra_voice/_version.py | head -n 1)
    if [[ $generated != "$version" ]]; then
        echo "ПРЕДУПРЕЖДЕНИЕ: _version.py содержит $generated; генерируется packaging/build-deb.sh из changelog, пересоберётся."
    else
        echo 'OK: _version.py согласован с версией.'
    fi
else
    echo 'OK: _version.py отсутствует; его создаст сборка.'
fi

if $skip_ci; then
    echo 'ПРЕДУПРЕЖДЕНИЕ: CI НЕ проверен (--skip-ci-check)'
elif ! command -v gh >/dev/null 2>&1 || ! gh auth status >/dev/null 2>&1; then
    check 'gh отсутствует или не авторизован; установите/авторизуйте gh или повторите с --skip-ci-check' false
else
    runs=$(gh run list --commit "$head" --workflow ci.yml --json status,conclusion 2>/dev/null) || runs=''
    if [[ -n $runs ]] && python3 -c 'import json,sys; r=json.load(sys.stdin); sys.exit(not (isinstance(r,list) and r and all(x.get("status") == "completed" and x.get("conclusion") == "success" for x in r)))' <<< "$runs"; then
        check 'все запуски CI на HEAD завершились успешно' true
    else
        check 'CI на HEAD отсутствует, выполняется или завершился с ошибкой' false
    fi
fi

if git rev-parse -q --verify "refs/tags/$tag" >/dev/null 2>&1; then
    check "локальный тег $tag уже существует" false
else
    check "локальный тег $tag свободен" true
fi
if $push; then
    remote_status=0
    git ls-remote --exit-code --tags origin "refs/tags/$tag" >/dev/null 2>&1 || remote_status=$?
    case $remote_status in
        0) check "удалённый тег $tag уже существует" false ;;
        2) check "удалённый тег $tag свободен" true ;;
        *) check 'не удалось проверить удалённый тег' false ;;
    esac
fi
if ((failed)); then exit 1; fi

cat <<'ASSETS'
Ассеты, которые опубликует job release:
  astra-voice_*.deb
  sbom.cdx.json
  SHA256SUMS
  SHA256SUMS.asc
  INSTALL-ADMIN.md (из docs/INSTALL-ADMIN.md)
  latest.json
  release.gpg (из data/keys/release.gpg)
ASSETS
if $skip_ci; then echo 'ПРЕДУПРЕЖДЕНИЕ: CI НЕ проверен (--skip-ci-check)'; fi
if [[ -n $(git config user.signingkey || true) ]]; then tag_kind=-s; else tag_kind=-a; fi
printf 'git tag %s %s -m %q\n' "$tag_kind" "$tag" "Astra Voice $tag"
printf 'git push origin refs/tags/%s\n' "$tag"
if ! $push; then
    echo 'dry-run: ничего не создано'
    exit 0
fi
if ! git tag "$tag_kind" "$tag" -m "Astra Voice $tag"; then
    echo "ОШИБКА: не удалось создать тег $tag" >&2
    exit 1
fi
if ! git push origin "refs/tags/$tag"; then
    echo "ОШИБКА: не удалось отправить тег $tag; локальный тег создан. Для удаления: git tag -d $tag" >&2
    exit 1
fi
echo 'Далее CI выполнит job release; Environment release ожидает одобрения ревьюера.'
