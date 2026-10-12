# S2 — спайк polkit-помощника (M0, трек A обновлятора)

Проверяем допущение: **из GUI можно поставить `.deb` через `pkexec` + собственное
polkit-действие, показав ровно одно окно пароля**, а помощник при этом соблюдает
все 10 условий приёма из `docs/threat-model.md` §4.1.

Ссылки: `docs/plans.md` M0.S2 и M8 · `arch/plan-synth.md` §7 И8/Н5/О3 ·
`arch/plan-claude.md` §3 `helper/update_helper.py` · `docs/threat-model.md` §4.1, §6
(T-01…T-07, T-17…T-19, T-27…T-30).

---

## 1. Что заказчик делает руками — одна команда

```
sudo sh "/home/astra/Документы/astra-voice/spikes/s2_polkit_helper/install-spike.sh"
```

Скрипт кладёт в систему ровно это (и печатает проверку в конце):

| # | Что | Куда | Права |
|---|---|---|---|
| 1 | `update-helper` | `/usr/libexec/astra-voice/update-helper` | root:root 0755 |
| 2 | `io.github.arrivarus.astra_voice.update.policy` | `/usr/share/polkit-1/actions/` | root:root 0644 |
| 3 | `keys/test-release.gpg` | `/usr/share/astra-voice/keys/test-release.gpg` | root:root 0644 |
| 4 | — | `/var/lib/astra-voice/staging` | root:root 0700 |
| 5 | тестовый пакет `astra-voice-spike` 0.0.1 | `apt-get install ./…0.0.1…deb` | — |

Больше ничего в систему не попадает: `/var/lib/astra-voice/updates.log` и
`update.lock` появятся при первом запуске помощника, и их тоже снимает откат.

**Откат — одна команда:**

```
sudo sh "/home/astra/Документы/astra-voice/spikes/s2_polkit_helper/uninstall-spike.sh"
```

Снимает пакет (`apt-get purge`), помощник, `.policy`, keyring, `staging`, журнал и
пустые каталоги; в конце печатает построчную проверку «нет: …».

---

## 2. Что потом запускает Юрка (в живой сессии, не раньше)

```
python3 "/home/astra/Документы/astra-voice/spikes/s2_polkit_helper/gui_probe.py"
```

`gui_probe.py` — PyQt5-окно с одной кнопкой; по кнопке `QProcess` запускает

```
pkexec /usr/libexec/astra-voice/update-helper install <…/packages/astra-voice-spike_0.0.2_all.deb>
```

Что снимать (критерии S2 из `docs/plans.md`):

- [ ] **ровно одно** окно пароля polkit (скриншот — в `arch/spikes.md`);
- [ ] счётчик «GUI жив» в окне продолжает расти, пока висит диалог (GUI не блокируется);
- [ ] коды различимы: **0** (помощник отработал), **126** (диалог отменён), **127** (нет
      агента/действия/пути) — подпись кода печатается в окне и в stdout;
- [ ] `dpkg-query -W astra-voice-spike` → `0.0.2`;
- [ ] в `/var/lib/astra-voice/updates.log` — одна строка `result=ok … version=0.0.2`;
- [ ] `journalctl -t astra-voice-update-helper -n 5` — та же строка в syslog (T-27);
- [ ] повтор той же установки → код **70** `downgrade`, apt не вызван (T-07);
- [ ] `apt-get install -y --no-remove ./x.deb` поставил локальный пакет **без**
      `--allow-unauthenticated` (видно в аргументах помощника и в журнале apt);
- [ ] при удержанном `/var/lib/dpkg/lock-frontend` — код **72** `apt-locked`, apt не убит (T-18);
- [ ] то же во Fly после перелогина (S17-A5: есть ли там polkit-агент).

Провал (нет агента polkit / второе окно): запасной путь — `pkcon install-local`,
затем трек B (`sudo apt install ./…` руками администратора), см. plans.md M0.S2.

> Урок `.patches/002`: `pkexec` и polkit-агент живут на **D-Bus**, а не на X11.
> Поэтому зонд запускается только в реальной сессии заказчика и только по команде —
> изоляцией `DISPLAY` тут ничего не проверить. Всё, что можно было проверить без
> шины и без root, проверено unit-тестами (раздел 4).

---

## 3. Помощник: что он делает по шагам

`update-helper`, shebang `#!/usr/bin/python3 -IS`, только stdlib
(`errno fcntl hashlib json os re stat subprocess sys time syslog`), `sys.path` не правится.

1. `os.environ.clear()` → фиксированный набор
   (`PATH=/usr/sbin:/usr/bin:/sbin:/bin`, `HOME=/root`, `SHELL=/bin/false`,
   `LC_ALL=LANG=C.UTF-8`, `DEBIAN_FRONTEND=noninteractive`). `PATH` из окружения
   вызывающего не используется вообще: все программы — по абсолютным путям.
2. argv строго `install <абсолютный нормализованный путь к *.deb>`; иначе код 64,
   ни одного подпроцесса.
3. `policy.conf` и `digsig_initramfs.conf` читаются **как данные** (`O_NOFOLLOW`,
   обычный файл, владелец root, без `w` для group/other; иначе файл игнорируется с
   пометкой `policy-ignored`). `updates=admin` → `policy-denied`;
   `DIGSIG_ELF_MODE≠0` → `policy-denied`; `updates=offline` → к apt добавляется
   `--no-download` (Н5).
4. Каталог-источник открывается как fd (`O_DIRECTORY|O_NOFOLLOW`), три файла —
   `<имя>.deb`, `SHA256SUMS`, `SHA256SUMS.asc` — читаются относительно этого fd с
   `O_NOFOLLOW` (symlink → `ELOOP` → `bad-input`).
5. `staging` = `/var/lib/astra-voice/staging`: `O_DIRECTORY|O_NOFOLLOW` + `fstat`
   (uid 0, режим ровно 0700), иначе `bad-staging`. Все три файла копируются в
   staging **из уже прочитанных байт** (0644). В спайке помощник создаёт staging при
   первом запуске; в продукте каталог кладёт `.deb`.
6. `gpgv --status-fd 1 --keyring <абс> SHA256SUMS.asc SHA256SUMS`. Успех = код 0
   **и** есть `VALIDSIG` **и** fingerprint (подписи и первичного ключа) ∈
   `PINNED_FINGERPRINTS` **и** ∉ `REVOKED_FINGERPRINTS`. Отзыв `gpgv` не проверяет —
   его даёт наш список (T1 §4.2, зафиксировано тестом `test_t05_revoked_key`).
7. sha256 из `SHA256SUMS` — разбирается из **тех же байт**, что ушли в `gpgv` (T-30);
   ровно одна строка на имя файла, иначе `bad-checksum`.
8. `dpkg-deb --field` по копии в staging: `Package=astra-voice-spike`
   (в продукте `astra-voice`), `Architecture ∈ {all, amd64}`.
9. `dpkg-query -W` + `dpkg --compare-versions <новая> gt <установленная>` → только
   повышение, иначе `downgrade`.
10. `apt-get install -y --no-remove ./<имя>.deb` (+`--no-download` при offline),
    cwd = staging, фиксированный argv, **без** `--allow-unauthenticated`.
11. Журнал: строка в `/var/lib/astra-voice/updates.log` (0644) и в syslog
    (`astra-voice-update-helper`, `LOG_AUTH`) — время, результат, пакет, версия,
    первые 12 символов sha256; без путей `$HOME` и без содержимого пакета (T-27).
    В stdout — одна строка JSON для GUI.

Параллельный запуск: `flock` на `/var/lib/astra-voice/update.lock` → второй
экземпляр завершается кодом `busy` (T-29).

### Коды выхода

| Код | Имя | Когда |
|---|---|---|
| 0 | `ok` | установка выполнена |
| 64 | `usage` | неверный argv |
| 65 | `bad-input` | нет файла / не обычный файл / symlink (`ELOOP`) / слишком большой |
| 66 | `bad-staging` | staging не root:root 0700 либо symlink |
| 67 | `bad-signature` | подпись не проверена / ключ не в пине / ключ отозван |
| 68 | `bad-checksum` | sha256 не совпал либо имя не найдено (или дублируется) в `SHA256SUMS` |
| 69 | `bad-package` | `Package`/`Architecture` не те |
| 70 | `downgrade` | версия не выше установленной |
| 71 | `policy-denied` | `updates=admin` либо `DIGSIG_ELF_MODE≠0` |
| 72 | `apt-locked` | удержан `/var/lib/dpkg/lock-frontend`, apt не убит |
| 73 | `apt-failed` | apt-get вернул ошибку |
| 74 | `busy` | работает другой экземпляр помощника |
| 75 | `internal` | внутренняя ошибка |

**126** и **127** принадлежат `pkexec` (отказ авторизации / не удалось запустить) —
помощник их не занимает, поэтому в GUI они однозначны.

---

## 4. Тесты (без root, без pkexec, без D-Bus)

```
~/.cache/astra-voice-spike/s2venv/bin/python -m pytest \
    "/home/astra/Документы/astra-voice/spikes/s2_polkit_helper/tests" -q
```

(pytest в системе не установлен; venv спайка — в scratch `~/.cache/astra-voice-spike/s2venv`,
вне репозитория и вне системы.)

Тестовый режим помощника (условие §4.1 п.10) включается **не через окружение** —
окружение помощник стирает, — а по расположению: помощник запущен как
`<ROOT>/usr/libexec/astra-voice/update-helper` и в том же дереве лежит флаг-файл
`<ROOT>/etc/astra-voice/SPIKE-TEST-MODE`. Тогда все абсолютные пути берутся с
префиксом `<ROOT>`, а требуемый владелец staging — текущий uid. Установленный по
каноническому пути помощник тестовый режим включить не может.

В chroot-каталоге лежат: обёртки над настоящими `gpgv`, `dpkg-deb`, `dpkg`
(записывают argv/cwd/env и зовут настоящую программу) и полные фейки `apt-get`,
`dpkg-query`. Покрытие: T-03 (10 вариантов argv), T-04 (враждебное окружение:
`PYTHONPATH`, `LD_PRELOAD`, `GNUPGHOME`, `PATH` с ловушками — ни одна не сработала,
env подпроцессов равен фиксированному набору), T-05 (чужой ключ / отозванный ключ /
второй ключ keyring), T-07 (downgrade и равная версия), T-01 (symlink вместо `.deb`),
T-02 (staging — symlink и режим 0755), T-17 (`updates=admin`, `DIGSIG_ELF_MODE=1`),
T-19 (`policy.conf` 0666 → игнорируется), T-18 (`apt-locked`), T-27 (журнал),
T-29 (`busy`), T-30 (дублирующаяся строка SUMS), плюс `--no-download` при offline и
проверка shebang/stdlib-only.

Отдельно проверено вручную: `/usr/bin/python3 -IS -c "import json,hashlib,subprocess,fcntl,syslog"`
на этой машине работает — stdlib грузится без `site` (пункт чеклиста S2).

---

## 5. Тестовые ключи и пакеты

Ключи спайка (Ed25519, `gpg --batch --quick-gen-key`) живут в scratch
`~/.cache/astra-voice-spike/gpg/`, **в репозиторий уходят только публичные части**:

| Роль | Fingerprint | Где |
|---|---|---|
| S1 (подписывает `SHA256SUMS`) | `CB95 1AD7 9440 7972 A0B1 A5BB AFA8 7398 C495 3A71` | keyring + пин |
| S2 (плановая ротация) | `5364 8D58 F2B0 3C06 CCE8 0580 E137 446C 3835 E961` | keyring + пин |
| revoked (был пиннут, отозван) | `4BAC A9A5 099D 20E1 3480 D667 5A12 55A8 2104 909F` | keyring + пин + `REVOKED_FINGERPRINTS` |
| foreign (чужой) | `38E5 590B 46CB 3453 7F92 AB7F C966 5F94 364F CE20` | нигде |

`keys/test-release.gpg` = S1 + S2 + revoked. Это **тестовые** ключи спайка, к
релизным ключам продукта (мастер + S1 + S2, T1 §4.2) отношения не имеют.

`packages/` — два пакета `astra-voice-spike` 0.0.1 и 0.0.2 (`dpkg-deb -b`, внутри
только `/usr/share/doc/astra-voice-spike/VERSION`), общий `SHA256SUMS` на оба файла
(0.0.1 нужен в сумме, чтобы дойти до проверки версии в тесте downgrade) и
`SHA256SUMS.asc` — подпись S1.

Фикстуры подписи для тестов пересобираются `sh tests/make_fixtures.sh` (требует
scratch-каталог ключей).

---

## 6. Состав каталога

```
update-helper                                    помощник (ставится в /usr/libexec/astra-voice/)
io.github.arrivarus.astra_voice.update.policy    polkit-действие auth_admin, exec.path, allow_gui
keys/test-release.gpg                            keyring спайка (S1 + S2 + revoked)
packages/                                        0.0.1, 0.0.2, SHA256SUMS, SHA256SUMS.asc
gui_probe.py                                     PyQt5 + QProcess + pkexec (запускает Юрка)
install-spike.sh / uninstall-spike.sh            единственный sudo-шаг и полный откат
tests/test_helper.py                             35 тестов, без root и без D-Bus
tests/make_fixtures.sh                           пересборка фикстур подписи
```

---

# Живой прогон (этап 2) — 2026-09-09

Заказчик выполнил `install-spike.sh` (один `sudo`), Юрка передал допуск на прогон зонда на
реальном дисплее. Пароль в диалоге polkit вводил **заказчик**; исполнитель пароль не вводил,
не читал и не сохранял.

## Состояние системы до прогона

```
/usr/libexec/astra-voice/update-helper                                  -rwxr-xr-x root root 23510
/usr/share/polkit-1/actions/io.github.arrivarus.astra_voice.update.policy -rw-r--r-- root root 1197
/usr/share/astra-voice/keys/test-release.gpg                            -rw-r--r-- root root   754
/var/lib/astra-voice/staging                                            drwx------ root root
dpkg-query -W astra-voice-spike → 0.0.1
polkit-агент: /usr/lib/x86_64-linux-gnu/libexec/polkit-kde-authentication-agent-1 (pid 3737)
```

## Прогон

```sh
python3 gui_probe.py packages/astra-voice-spike_0.0.2_all.deb     # окно зонда, кнопка нажата 15:52:19
```

`out/live_s2_gui.log` (JSON помощника — `out/live_s2.json`):

```json
{"result": "ok", "exit": 0, "message": "установлено", "policy": "policy-absent",
 "fingerprint": "CB951AD794407972A0B1A5BBAFA87398C4953A71",
 "sha256": "738d6f2c1b5522a3f5edf92d39c9f7c704d287beb0a5cfe1a3d553edf89743af",
 "package": "astra-voice-spike", "version": "0.0.2", "installed": "0.0.1"}
exit=0  0 — помощник отработал (см. JSON в stdout)
```

Журнал помощника `/var/lib/astra-voice/updates.log` (копия — `out/live_s2_updates.log`):

```
2026-09-09T15:52:24+0300 result=ok exit=0 package=astra-voice-spike version=0.0.2
  installed=0.0.1 sha256=738d6f2c1b55 fingerprint=CB951AD794407972A0B1A5BBAFA87398C4953A71 msg=установлено
```

**От нажатия кнопки до установки — 5 секунд**, включая ввод пароля заказчиком.

## Чеклист M0.S2

| Критерий `docs/plans.md` | Факт | Вердикт |
|---|---|---|
| Ровно **одно** окно ввода пароля | опрос окон класса `polkit` раз в секунду весь прогон: одновременно максимум **1**, уникальных за прогон **1** (`0x2400 00c`, id 37748748) | **PASS** |
| Код **0** различим | `exit=0`, JSON `result=ok` | **PASS** |
| Коды **126 / 127** различимы | живьём **не проверялись** — каждый потребовал бы ещё одного диалога у заказчика; разобраны в `gui_probe.py` (`EXIT_HINTS`) и в §«Коды выхода», покрыты юнит-тестами | **не проверено живьём** |
| `dpkg-query -W` = 0.0.2 | `astra-voice-spike 0.0.2` | **PASS** |
| GUI не блокируется | `QProcess` асинхронный, тикер «GUI жив: N» крутился в окне; кнопка снова стала активной после `finished` | **PASS**, но инструментально (скриншотом) не зафиксировано |
| Установка локального `.deb` **без** `--allow-unauthenticated` (T1 §5) | `run_apt`: `argv = [apt-get, install, -y, --no-remove, ./<staged>]`, флага нет в коде установленного помощника; установка прошла | **PASS** |
| `apt-locked` (удержанный `lock-frontend`) | сымитировать без root нельзя (lock держит root) | **не проверено живьём**, есть юнит-тест (`tests/test_helper.py`, код 72) |
| `--no-download` при offline | ветка включается по `policy.conf`; файла нет → `policy-absent`, offline=false | **не проверено живьём**, есть юнит-тест (`tests/test_helper.py`) |

## Побочные факты

* `/var/lib/astra-voice/` после прогона: `staging` (0700 root, содержимое недоступно
  непривилегированному пользователю — как и задумано), `update.lock` (0600), `updates.log` (0644).
* Правка спайка в этом прогоне: `gui_probe.py` дублирует stdout помощника в консоль — иначе JSON
  оставался только в окне и не попадал в артефакты.
* Юнит-тесты (35 шт.) **сейчас прогнать нечем**: `pytest` не установлен ни в системе, ни в обоих
  scratch-venv. Ставить пакеты в рамках прогона нельзя — вопрос человеку.
