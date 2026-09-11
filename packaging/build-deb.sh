#!/bin/bash
# Сборка astra-voice_*.deb. Сеть нужна только режиму --fetch-wheels.
#
#   packaging/build-deb.sh                 # авто: контейнер, если есть; иначе --host
#   packaging/build-deb.sh --container     # поднять debian:12 и собрать в нём (dh + lintian)
#   packaging/build-deb.sh --dh            # мы УЖЕ внутри debian:12 (так работает CI)
#   packaging/build-deb.sh --host          # запасной путь: dpkg-deb по готовому дереву
#   packaging/build-deb.sh --no-vendor     # без onnxruntime/onnx_asr (ELF в пакете = 0)
#   packaging/build-deb.sh --fetch-wheels  # СЕТЬ: наполнить кэш колёс по wheels.lock
#
# Раскладка задаётся ровно в одном месте — packaging/debian/rules, цель
# override_dh_auto_install; режим --host вызывает ту же цель с другим $PKG,
# чтобы дерево пакета не разъезжалось между путями сборки.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$ROOT/.build"
DIST="$ROOT/dist"
WHEEL_CACHE="${ASTRA_VOICE_WHEEL_CACHE:-$HOME/.cache/astra-voice-dev/wheels}"
LOCK="$ROOT/packaging/wheels.lock"

MODE=auto
VENDOR=1
FETCH=0
for arg in "$@"; do
	case "$arg" in
	--container) MODE=container ;;
	--dh) MODE=dh ;;
	--host) MODE=host ;;
	--no-vendor) VENDOR=0 ;;
	--fetch-wheels | --download-wheels) FETCH=1 ;;
	-h | --help)
		sed -n '2,13p' "${BASH_SOURCE[0]}"
		exit 0
		;;
	*)
		echo "неизвестный аргумент: $arg" >&2
		exit 2
		;;
	esac
done

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
die() {
	printf '\033[31mОШИБКА: %s\033[0m\n' "$*" >&2
	exit 1
}

VERSION="$(sed -n '1s/.*(\(.*\)).*/\1/p' "$ROOT/packaging/debian/changelog")"
[ -n "$VERSION" ] || die "не разобрал версию из packaging/debian/changelog"
# Воспроизводимость. Источник времени — дата из changelog: она есть везде,
# в отличие от git (в контейнере CI `git log` спотыкается на dubious ownership,
# а откат на `echo 1` давал файлы 1970 года и 518 ошибок lintian
# `package-contains-ancient-file`).
if [ -z "${SOURCE_DATE_EPOCH:-}" ]; then
	CHANGELOG_DATE="$(sed -n 's/^ -- .*>  //p' "$ROOT/packaging/debian/changelog" | head -1)"
	SOURCE_DATE_EPOCH="$(date -u -d "$CHANGELOG_DATE" +%s 2>/dev/null || true)"
fi
if [ -z "${SOURCE_DATE_EPOCH:-}" ]; then
	SOURCE_DATE_EPOCH="$(git -C "$ROOT" log -1 --format=%ct 2>/dev/null || date +%s)"
fi
export SOURCE_DATE_EPOCH
DEB="astra-voice_${VERSION}_amd64.deb"

# --- колёса (единственный шаг с сетью) ---------------------------------------
if [ "$FETCH" = 1 ]; then
	say "качаю колёса по $LOCK в $WHEEL_CACHE"
	mkdir -p "$WHEEL_CACHE"
	python3 -m pip download --no-deps --only-binary=:all: --require-hashes \
		--dest "$WHEEL_CACHE" -r "$LOCK"
	say "готово; дальше сеть не нужна"
	exit 0
fi

# --- выбор режима -------------------------------------------------------------
CONTAINER=""
if [ "$MODE" = auto ] || [ "$MODE" = container ]; then
	for engine in podman docker; do
		if command -v "$engine" >/dev/null 2>&1 && "$engine" info >/dev/null 2>&1; then
			CONTAINER="$engine"
			break
		fi
	done
	if [ -n "$CONTAINER" ]; then
		MODE=container
	elif [ "$MODE" = container ]; then
		die "ни podman, ни docker не доступны (docker без прав? нужна группа docker)"
	else
		say "контейнерного движка нет — запасной режим --host (без dh и lintian)"
		MODE=host
	fi
fi

mkdir -p "$DIST" "$BUILD"

# --- контейнер: всё то же самое, но внутри debian:12 --------------------------
if [ "$MODE" = container ]; then
	say "сборка в контейнере ($CONTAINER, debian:12)"
	"$CONTAINER" build -t astra-voice-build -f "$ROOT/packaging/Containerfile" "$ROOT"
	exec "$CONTAINER" run --rm \
		-v "$ROOT:/src:Z" \
		-v "$WHEEL_CACHE:/wheels:ro,Z" \
		-e SOURCE_DATE_EPOCH \
		-e ASTRA_VOICE_WHEEL_CACHE=/wheels \
		-w /src astra-voice-build \
		packaging/build-deb.sh --dh $([ "$VENDOR" = 1 ] || echo --no-vendor)
fi

# --- версия для кода ----------------------------------------------------------
say "версия $VERSION (SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH, режим $MODE)"
cat >"$ROOT/src/astra_voice/_version.py" <<EOF
# Сгенерировано packaging/build-deb.sh из packaging/debian/changelog. Не править.
__version__ = "$VERSION"
EOF

# --- vendor -------------------------------------------------------------------
VENDOR_DIR=""
if [ "$VENDOR" = 1 ]; then
	[ -d "$WHEEL_CACHE" ] || die "нет кэша колёс $WHEEL_CACHE — запустите с --fetch-wheels (нужна сеть) или соберите с --no-vendor"
	VENDOR_DIR="$BUILD/vendor"
	rm -rf "$VENDOR_DIR"
	mkdir -p "$VENDOR_DIR"
	say "vendor: pip install --require-hashes из $WHEEL_CACHE"
	python3 -m pip install --no-deps --no-index --find-links "$WHEEL_CACHE" \
		--require-hashes -r "$LOCK" --target "$VENDOR_DIR" --quiet
	for vendor_patch in "$ROOT"/vendor/patches/*.patch; do
		[ -f "$vendor_patch" ] || continue
		if patch_output="$(cd "$VENDOR_DIR" && LC_ALL=C patch -p1 --dry-run --forward --batch < "$vendor_patch" 2>&1)"; then
			(cd "$VENDOR_DIR" && patch -p1 --forward --no-backup-if-mismatch --batch < "$vendor_patch") ||
				die "не удалось применить $vendor_patch к vendor"
			say "vendor: применён патч $(basename "$vendor_patch")"
		elif [[ "$patch_output" == *"Reversed (or previously applied)"* ]] &&
			(cd "$VENDOR_DIR" && patch -p1 --dry-run --reverse --batch < "$vendor_patch" >/dev/null 2>&1); then
			say "vendor: патч $(basename "$vendor_patch") уже применён, пропускаю"
		else
			die "патч $vendor_patch разошёлся с пином onnx-asr из wheels.lock: $patch_output"
		fi
	done
	find "$VENDOR_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} +
	rm -rf "$VENDOR_DIR"/bin
else
	say "vendor пропущен (--no-vendor): в пакете не будет onnxruntime, ELF = 0"
fi
export ASTRA_VOICE_VENDOR="$VENDOR_DIR"

# --- сборка -------------------------------------------------------------------
if [ "$MODE" = dh ]; then
	command -v dpkg-buildpackage >/dev/null || die "нет dpkg-buildpackage: режим --dh рассчитан на образ packaging/Containerfile"
	say "dpkg-buildpackage -us -uc -b"
	rm -rf "$ROOT/debian"
	cp -a "$ROOT/packaging/debian" "$ROOT/debian"
	(cd "$ROOT" && dpkg-buildpackage -us -uc -b)
	rm -rf "$ROOT/debian"
	mv "$ROOT/../$DEB" "$DIST/$DEB" 2>/dev/null || mv "$(dirname "$ROOT")/$DEB" "$DIST/$DEB"
	if command -v lintian >/dev/null; then
		say "lintian (ошибки — стоп, предупреждения — в лог)"
		lintian --fail-on error --suppress-tags-from-file /dev/null "$DIST/$DEB" || die "lintian нашёл ошибки"
	fi
else
	say "сборка на хосте: dpkg-deb по дереву из packaging/debian/rules"
	STAGE="$BUILD/pkg"
	rm -rf "$STAGE"
	mkdir -p "$STAGE"
	# Та же цель, что у dh: раскладка описана один раз.
	make -C "$ROOT" -f packaging/debian/rules override_dh_auto_install \
		PKG="$STAGE" ASTRA_VOICE_VENDOR="$VENDOR_DIR"
	install -d -m 0755 "$STAGE/var/lib/astra-voice"
	install -d -m 0700 "$STAGE/var/lib/astra-voice/staging"
	install -d -m 0755 "$STAGE/DEBIAN"
	python3 "$ROOT/packaging/make_control.py" \
		--control "$ROOT/packaging/debian/control" \
		--version "$VERSION" \
		--tree "$STAGE" \
		--out "$STAGE/DEBIAN/control"
	(cd "$STAGE" && find . -path ./DEBIAN -prune -o -type f -print0 |
		xargs -0 md5sum 2>/dev/null | sed 's| \./| |' >DEBIAN/md5sums) || true
	find "$STAGE" -newermt "@$SOURCE_DATE_EPOCH" -print0 |
		xargs -0 -r touch --no-dereference --date="@$SOURCE_DATE_EPOCH"
	fakeroot dpkg-deb --root-owner-group -Zxz --build "$STAGE" "$DIST/$DEB"
fi

[ -f "$DIST/$DEB" ] || die "пакет не собрался: $DIST/$DEB"

# --- гейты --------------------------------------------------------------------
say "гейт sha256: модель VAD"
(cd "$ROOT/data/vad" && sha256sum -c SHA256SUMS)

say "гейт ELF: tools/elf-audit --strict"
EXPECT=$([ "$VENDOR" = 1 ] && echo 3 || echo 0)
python3 "$ROOT/tools/elf-audit" --strict --expect "$EXPECT" "$DIST/$DEB"

if [ "$VENDOR" = 1 ]; then
	say "гейт Р3: METADATA колёс ↔ apt и Depends"
	python3 "$ROOT/tools/deps-audit" --vendor "$VENDOR_DIR" --deb "$DIST/$DEB" --strict
fi

say "SBOM"
python3 "$ROOT/scripts/sbom.py" --deb "$DIST/$DEB" --out "$DIST/sbom.cdx.json"

say "готово: $DIST/$DEB ($(du -h "$DIST/$DEB" | cut -f1), режим $MODE, vendor=$VENDOR)"
