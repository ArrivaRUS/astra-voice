# GCC source coverage для AppImage — 09.10.2026

Два соответствующих CentOS SRPM получены после явного разрешения владельца
09.10.2026, снявшего запрет 29.09 для этих двух файлов. Они проверены и закреплены
как `source` в `packaging/appimage.lock`, вместо двух `pending-source`.
Принятый формат уже поддерживает самостоятельный SRPM: `debverify.py sources`
проверяет размер/SHA256, а `scripts/release_sources.py` включает каждый `source`
в `upstream/<id>/<file>`. Новый формат lock или механизм скачивания не добавлен.

Это подтверждает получение и включение исходников в план сборки. Окончательный
архив исходников выпуска из финального коммита ещё не собран и не проверен.
Рабочая копия содержит незакоммиченный merge: HEAD
`0c210c920e204ec575bcd06ce0cabc1a056ca51c`, MERGE_HEAD
`7890e73372f0b1063e5d12e708c8c525d879a807`. Проверку чистого дерева и `git archive`
в release builder не обходили, рабочие файлы не выдавали за содержимое коммита.

## Закреплённые пакеты и цепочка происхождения

| ID в lock | Source RPM | Размер, байт | SHA256 |
|---|---|---:|---|
| `gcc-libquadmath` | `gcc-4.8.5-44.el7.src.rpm` | 79314655 | `642c4085f6af9565e39583e54a9d7a5d9e6b93dd8c8e9474aec8a892d6c7aa36` |
| `gcc-libgfortran` | `gcc-libraries-8.3.1-2.1.1.el7.src.rpm` | 137336608 | `cf2a1d2c2e5a56eb2e495c2638e64769b73db07555e1f7ce34cbd9feff6891f5` |

Источники:

- [gcc-4.8.5-44.el7.src.rpm](https://vault.centos.org/7.9.2009/os/Source/SPackages/gcc-4.8.5-44.el7.src.rpm)
- [gcc-libraries-8.3.1-2.1.1.el7.src.rpm](https://vault.centos.org/7.9.2009/os/Source/SPackages/gcc-libraries-8.3.1-2.1.1.el7.src.rpm)
- [Source repomd.xml](https://vault.centos.org/7.9.2009/os/Source/repodata/repomd.xml)
  и его отдельная подпись `repomd.xml.asc`.

Автор повторно проверил `repomd.xml.asc` через `gpgv`: код 0, `VALIDSIG`
`6341AB2753D78A78A7C27BB124C6A8A7F4A80EB5`. Сжатый primary-индекс совпал по размеру
и SHA256 `e9ed33ce5da5e899a647de4849b8981c89f179d0b2c39f6294e2f257deedc3b6`
с подписанным repomd. Обе записи Source RPM в primary совпали с полученными
файлами по имени, размеру и SHA256. При копировании в
`~/.cache/astra-voice-dev/appimage/sources/` хэши проверены ещё раз.
Полная подпись CentOS здесь проверена на этапе установления происхождения;
обычная сборочная проверка самостоятельных `source` повторяет закреплённые
размеры/хэши и не реализует новый RPM-репозиторный verifier.

Соответствие библиотек закреплённому NumPy 1.24.2 (wheel SHA256
`9a23f8440561a633204a67fb44617ce2a299beecf3295f0d13c495518908e910`) взято из
предшествующего независимого отчёта `gcc-provenance-review.md`:

- `libquadmath-4.8.5-44.el7.x86_64.rpm` → `gcc-4.8.5-44.el7.src.rpm`,
  GNU Build ID `549b4c82347785459571c79239872ad31509dcf4`;
- `libgfortran5-8.3.1-2.1.1.el7.x86_64.rpm` →
  `gcc-libraries-8.3.1-2.1.1.el7.src.rpm`,
  GNU Build ID `5bbe74eb6855e0a2c043c0bec2f484bf3e9f14c0`.

Независимый анализ подтвердил совпадение кода, данных, релокаций и нормализованных
динамических символов (131/131 и 1617/1617 соответственно). SONAME/NEEDED и
служебные таблицы изменены при упаковке wheel. Здесь этот ELF-анализ заново не
выполнялся. Полное воспроизведение wheel repair, версии его инструментов и
побайтовое воспроизведение сборки не заявляются.

## Содержимое SRPM

Проверка использовала `bsdtar 3.8.4`: список членов, чтение spec через `-xOf`,
потоковый список вложенного исходного архива. Пакеты не устанавливались, RPM
макросы, `%prep`, `%build`, `%install`, патчи и исходный код не исполнялись.
Верхнеуровневые имена уникальны и плоские; обращения к путям вне архива не нужны.

| Пакет | Состав | Исходник нужной библиотеки |
|---|---|---|
| gcc 4.8.5 | 141 член: `gcc.spec`, 4 используемых Source, 136 Patch | `gcc-4.8.5-20150702.tar.bz2`, дерево `libquadmath/` (135 членов) |
| gcc-libraries 8.3.1 | 37 членов: `gcc-libraries.spec`, 3 Source, 33 Patch | `gcc-8.3.1-20190223.tar.xz`, дерево `libgfortran/` (789 членов) |

Все имена патчей из обеих spec присутствуют в соответствующих SRPM. Все
применимые Source также присутствуют. В `gcc.spec` дополнительно объявлен
условный `Source10` для Java bootstrap; он не включён в SRPM и не требуется для
этой CentOS 7 сборки: `build_java=0` при `rhel>=7`, а bootstrap отдельно opt-in.
Это исключение учтено явно, без утверждения, что доступен каждый произвольный
вариант spec. `NoSource`/`NoPatch` не обнаружены.

GCC 4.8.5 также включает исходники isl 0.11.1, cloog 0.18.0 и fastjar 0.97;
gcc-libraries — mpc 0.8.1 и дополнительный GCC 7.3.1 snapshot. Основные архивы
содержат 69316 и 85501 членов. В обоих деревьях нужных библиотек проверены
`configure`, `Makefile.am`, `Makefile.in`; spec содержит `%prep`, `%build`,
`%install` и объявления сборочных зависимостей. Рецепты и входные материалы
присутствуют; это не проверка сборки в восстановленном CentOS окружении.

## Изменения и проверки

`SBOM` для двух GCC runtime сохраняет лицензии и добавляет `source-distribution`,
`source-file`, `source-sha256`, `sources=pinned`. Общий механизм `pending-source`
сохранён для других входов. `INDEX.txt` лицензий больше не получает прежнее
предупреждение о двух GCC-библиотеках. NOTICE и текущий абзац R3.6 обновлены;
ранее уточнённая правовая формулировка GCC Runtime Exception сохранена.

Фактически выполнены:

- Python `hashlib.file_digest(..., 'sha256')`, сверка размеров обоих SRPM;
  `gpgv --status-fd 1 --keyring <centos7.gpg> repomd.xml.asc repomd.xml`,
  проверка primary и двух записей Source через `xml.etree.ElementTree` — PASS.
- `bsdtar -tf <SRPM>`, `bsdtar -xOf <SRPM> <spec>`, затем `bsdtar -xOf <SRPM>
  <source archive>` с передачей stdout в `bsdtar -tf -` через Python subprocess;
  сверка деклараций Source/Patch с членами — PASS.
- `python packaging/appimage/lockfile.py packaging/appimage.lock check` — PASS:
  13 source, 0 незакреплённых входов.
- `python packaging/appimage/debverify.py --lock packaging/appimage.lock --cache
  ~/.cache/astra-voice-dev/appimage --root . sources` — PASS, все 13 файлов.
- `pytest -q` для `test_appimage_debian.py`, `test_appimage_licenses.py`,
  `test_release_sources.py`, `test_release_sources_hardening.py` — 127 passed,
  0 skipped, 4.317 с. Только изолированные временные профили/синтетические архивы;
  DISPLAY/Wayland отключены, Qt offscreen, D-Bus и PulseAudio направлены в
  несуществующие сокеты.
- `ruff check` и `ruff format --check` трёх изменённых Python-файлов — PASS.
- Прямой `mypy` трёх файлов обнаружил две прежние ошибки в `scripts/sbom.py`
  (`epoch`/повторное объявление `bom` в .deb ветке). Обе воспроизведены на
  неизменённом файле из git index; в этот узкий sourcecoverage diff они не входят.

Первичные доказательства координатора: локальный каталог
`dist/appimage-final-validation/source-metadata/` и
`dist/appimage-final-validation/gcc-provenance-review.md`. Их ранние README
описывают момент до разрешения/загрузки, текущий статус фиксирует этот отчёт.
Авторские результаты и списки: `/tmp/av-gcc-coverage-afnhzshb/`
(`source-completeness.json`, `author-signature-verification.json`, списки
архивов, `packaging-tests.xml`). Крупные исходные архивы не добавлены в Git.

Осталось: независимое ревью этого diff и финальная сборка/проверка AppImage,
SBOM и release sources из согласованного коммита. Живые GUI/звук, установка
пакетов, сетевое скачивание других входов и публикация в этом этапе не выполнялись.
