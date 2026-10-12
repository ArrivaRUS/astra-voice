# E13 «Запись встреч»: какие ВКС-программы реально стоят на ALSE 1.8 и как детектору узнать звонок

Дата: 2026-09-26. Автор: researcher (Claude). Вход: `PRD.md` F18, `research/tech-meetings.md` §1.3, запрос заказчика
(CPO ГК «Астра») — список ВКС для приоритета поддержки детектором звонка.

**Бэкенд поиска.** Кредов Яндекс Search API (`tokens_and_keys.env`) на машине нет, поэтому по скиллу `market-research` —
откат на WebSearch/WebFetch и GitHub (raw-файлы). **В системе ничего не запускалось** (`pactl`, `pw-dump`, клиенты ВКС не
вызывались). Локальные факты получены только чтением:
- `/var/lib/dpkg/status`, `/usr/share/applications/*.desktop`, `~/.local/share/applications/*.desktop`;
- индексы APT `/var/lib/apt/lists/*Packages` — включая **корпоративный репозиторий ГК «Астра»**
  `artifactory.astralinux.ru/artifactory/gca-service-repo` (1.8, main);
- сохранённое состояние звуковых потоков: `~/.local/state/wireplumber/stream-properties` (PipeWire, текущее) и
  `~/.config/pulse/*-stream-volumes.tdb` (PulseAudio, исторические ключи через `strings`) — это **реальные имена потоков,
  которые уже появлялись на машине заказчика**;
- `strings` по бинарям `/opt/Dion/dion`, `/opt/Dion/resources/app.asar`.

Уверенность: **high** — первоисточник или прямое наблюдение на машине; **med** — вторичный источник или вывод из косвенных
признаков (зависимости пакета, исходники движка); **low** — единичное упоминание или экстраполяция. «данных нет» — искал и не нашёл.

Ограничение по решению заказчика 26.09 (`decisions/log.md`): запись — **только по кнопке**. Детектор лишь предлагает
«Записать встречу?», поэтому цена ложного срабатывания — лишнее уведомление, а не скрытая запись.

---

## 0. Главное

1. **На машине заказчика из ВКС нативно стоит только DION** (`dion` 5.34.1, Electron 42.3.3) — тот же DION, на который
   «Группа Астра» перевела внутренние встречи (≈533 ВКС в день). Остальное — браузеры (Chromium 143 «astragost»,
   Chromium-GOST, Яндекс.Браузер 25.10, Firefox 146), Telegram Desktop (ручная установка в `~/Загрузки`), Mattermost Desktop
   5.11.2 (есть плагин Calls), Psi+ (XMPP). (**high**, dpkg + .desktop)
2. **Корпоративный репозиторий ГК «Астра» раздаёт сотрудникам:** `dion`, `trueconf` (сборка `astralinux_se18`),
   `ivaconnect` (IVA, сборка `.astra`), `ktalk` (Контур.Толк 3.2.0), `zoom` 6.6, `mattermost-desktop`, `tag-desktop`
   (мессенджер «Магнита»), `Zoiper5` (SIP), `google-chrome-stable`. Это самый точный срез «что реально используют у заказчика»
   (**high** — индекс APT; толкование «gca = ГК Астра, служебный репозиторий» — **med**, по имени).
3. **Рынок (крупные компании, 2025, TelecomDaily, 1700+ респондентов, один ответ):** IVA 33 %, TrueConf 21 %, VK Звонки 16 %,
   Яндекс Телемост 9 %, DION 5 %, МТС Линк 5 %, SaluteJazz 4 %, иностранные суммарно 7 % (**med**; эксперты ComNews оспаривают
   «90 % отечественных» — называют 40–50 %). **По госсектору отдельных долей данных нет.**
4. **Техника детекта делится на три класса:**
   - **нативные с собственным звуковым стеком** (TrueConf, IVA Connect, Zoom, VK WorkSpace, Telegram) — узнаются по
     `application.process.binary`/`application.name`; самые надёжные, но точные имена потоков у большинства **не подтверждены**
     — нужен спайк с живым звонком;
   - **Electron** (DION, Контур.Толк, МТС Линк, MAX, Mattermost, Nextcloud Talk) — до Electron 43 **все называются «Chromium» /
     «Chromium input»**, различать можно только по `application.process.binary` (`dion`, `ktalk`…);
   - **браузер** (Телемост, VK Звонки web, SaluteJazz web, Jitsi, BBB, Teams/Zoom web) — поток = браузер. В Chromium-семействе
     узнать сайт по потоку **нельзя** (`media.name` = «Playback»/«RecordStream»); в Firefox ≥117 `media.name` содержит
     **заголовок вкладки** — сайт виден.
5. **Общий признак звонка для всех классов:** один и тот же процесс (по `application.process.binary`/PID) держит **одновременно
   захват микрофона (source-output) и вывод звука (sink-input)** дольше N секунд. Только вывод = YouTube/музыка; только захват =
   диктовка/проверка микрофона.

---

## 1. Что стоит на машине заказчика сейчас

| Программа | Пакет / путь | Версия | Откуда | Имя потока, уже встречавшееся на машине | Увер. |
|---|---|---|---|---|---|
| **DION** | `dion`, `/opt/Dion/dion`, `dion.desktop` (`StartupWMClass=Dion`) | 5.34.1-4479; Electron 42.3.3 / Chrome 148 | корп. репо (там же 5.29–5.34.0) | отдельного «Dion» в сохранённых потоках **нет** → при Electron 42 он пишется как `Chromium` / `Chromium input` (см. §3) | high (пакет), med (имя) |
| Chromium (astragost) | `chromium`, `/usr/lib/chromium/chromium` | 143.0.7499 | репо ALSE main (там уже 147) | вывод `application.name=Chromium`, захват `Chromium input` | high |
| Chromium-GOST | `chromium-gost` — **обёртка**, запускает тот же `chromium` с другим user-agent | 1.0.47r2 | ALSE main | те же `Chromium` / `Chromium input` | high |
| Яндекс.Браузер | `yandex-browser-stable`, бинарь `/opt/yandex/browser/yandex_browser` | 25.10.1 | ALSE **extended** | вывод `application.name=Yandex` (PipeWire и PulseAudio) | high |
| Firefox | `firefox` + `firefox-astra` | 146.0.1 | ALSE main | `application.name=Firefox` — и на вывод, и на захват | high |
| Telegram Desktop | `~/Загрузки/Telegram/Telegram` (не deb), `StartupWMClass=TelegramDesktop` | н/д | сайт | вывод `application.name=Telegram Desktop` (PulseAudio-история) | high |
| Mattermost Desktop | `mattermost-desktop`, `/opt/Mattermost/` | 5.11.2 (Electron) | корп. репо | не встречался | high |
| Psi+ | `psi-plus` (XMPP, Qt; Jingle-звонки) | 1.4.1523 | ALSE main | не встречался | high |
| TrueConf, IVA Connect, Контур.Толк, Zoom | **не установлены**, но есть в корп. репо | — | корп. репо | — | high |

Прочее в `stream-properties`: `astra-voice`, `astra-voice-spike-s5`, `ALSA plug-in [handy]`, `parec`/`parecord`,
`SimpleScreenRecorder` — это наши и служебные потоки; детектор должен их **исключать** (свой PID + список).

---

## 2. Таблица кандидатов

Колонки: **Клиент на Astra** — нативный deb/AppImage или только браузер; **Имена для детекта** — что искать в свойствах узла
PipeWire/PulseAudio (`application.process.binary` — самое стабильное); **Доля/внедрения**; **Как отличить звонок от звука**.

| # | Программа · вендор | Клиент на Astra 1.8 | Пакет / процесс / .desktop | Имена для детекта (PipeWire/Pulse) | Доля, реестр, внедрения | Особенности и отличие звонка |
|---|---|---|---|---|---|---|
| 1 | **DION** · ООО «Дион» (ИТ-холдинг Т1) | **нативный deb**, Electron; Astra 1.8+ в системных требованиях ([FAQ](https://faq.dion.vc/ru/users/requirements), med); на машине и в корп. репо (high) | `dion`; `/opt/Dion/dion`; `dion.desktop`, WM_CLASS `Dion` (high) | binary `dion` (med — libpulse берёт из `/proc/self/exe`); `application.name` при Electron 42 = `Chromium` / `Chromium input`, с Electron 43 — `Dion`/`Dion input` (med, §3) | 5 % крупного бизнеса ([TelecomDaily/Хабр](https://habr.com/ru/news/923634/), med); **основная ВКС «Группы Астра»**, ~533 ВКС/день ([TAdviser](https://www.tadviser.ru/index.php/%D0%A1%D1%82%D0%B0%D1%82%D1%8C%D1%8F:%D0%98%D1%80%D0%B8%D0%BD%D0%B0_%D0%A8%D0%B8%D1%80%D0%BE%D0%BA%D0%BE%D0%B2%D0%B0,_%D0%93%D1%80%D1%83%D0%BF%D0%BF%D0%B0_%D0%90%D1%81%D1%82%D1%80%D0%B0:_%D0%B2%C2%A0DION_%D0%BC%D1%8B_%D0%BF%D1%80%D0%BE%D0%B2%D0%BE%D0%B4%D0%B8%D0%BC_533%C2%A0%D0%B2%D0%B8%D0%B4%D0%B5%D0%BE%D0%BA%D0%BE%D0%BD%D1%84%D0%B5%D1%80%D0%B5%D0%BD%D1%86%D0%B8%D0%B8_%D0%B2_%D0%B4%D0%B5%D0%BD%D1%8C), med) | звонок = binary `dion` держит захват + вывод. Отдельный процесс → с браузером не путается. Звук уведомлений чата даёт вывод без захвата — не звонок. Есть и веб-вход (тогда класс «браузер») |
| 2 | **TrueConf** · ООО «Труконф» | **нативный deb** `trueconf_client_astralinux_se18` 8.5.2 в корп. репо (high); вендор заявляет совместимость клиента 8.4.1 с SE 1.8 (med); Qt-клиент (low) | `trueconf`; `/opt/trueconf/client/TrueConf` (med, AUR) | binary `TrueConf` (med); `application.name` — **данных нет** | 21 % крупного бизнеса (med); исторически основной в госсекторе: Росгвардия, ВКС на «Байкал-М»+Astra ([Коммерсантъ](https://www.kommersant.ru/doc/4955531), med); сервер в Ready for Astra ([astra.ru](https://astra.ru/about/press-center/news/server-videosvyazi-trueconf-mcu-sovmestim-s-os-astra-linux/), high) | нативный — различение простое. Ложное срабатывание: «проверка звука» перед звонком (захват + вывод). Нужен debounce ≥10 с или окно конференции |
| 3 | **IVA Connect** (клиент IVA MCU) · IVA Technologies | **нативный deb** `ivcs_messenger_desktop-18.4.4127.astra.deb` в корп. репо; на GStreamer (high — зависимости) | `ivaconnect`; бинарь — **данных нет** | GStreamer `pulsesrc/pulsesink` обычно ставит `application.name` = имя программы (low); точные имена — **данных нет** | **33 %, лидер** крупного бизнеса (med); IVA MCU 25.3 совместим с SE 1.8.2 ([IVA](https://iva.ru/ru/news/iva-technologies-i-gruppa-astra-podtverdili-sovmestimost-iva-mcu-i-astra-linux-se/), high); клиент для Astra с 2021 ([CNews](https://www.cnews.ru/news/line/2021-05-20_vypushchena_versiya_messendzhera), med) | нативный. Мессенджер с голосовыми → короткие захваты; debounce |
| 4 | **Яндекс Телемост** · Яндекс | **только браузер**: десктоп-клиент — Windows 10+/macOS 12+, Linux не указан ([Яндекс](https://yandex.ru/support/yandex-360/customers/telemost/desktop/ru/start/requirements), high) | вкладка `telemost.yandex.ru` в Яндекс.Браузере / Chromium / Firefox | `Yandex` + `Yandex input` (input — med, по исходникам Chromium), `Chromium`/`Chromium input`, `Firefox` (media.name = заголовок вкладки) | 9 % (med); реестр ПО (med, [anti-malware](https://www.anti-malware.ru/analytics/Market_Analysis/Russian-video-conferencing-2025)) | класс «браузер» (§4). ⚠ Не путать с **«Телемост 2.0»** (mytelemost.ru) — другая on-prem ВКС с deb, сертифицирована на Astra 1.7 ([mytelemost.ru](https://mytelemost.ru/sertifikacziya-na-sovmestimost-s-astra-linux-1-7/), med) |
| 5 | **VK Звонки / VK WorkSpace** · VK Tech | веб `calls.vk.com`; **нативный deb/rpm** суперапп VK WorkSpace (бывш. VK Teams), Linux вкл. Astra/РЕД ОС ([VK](https://workspace.vk.ru/docs/saas/user-guides/vk-teams/installation/linux-installation/index.html), med) | `vkteams` (по примеру команды установки, med); процесс — **данных нет** | нативный — **данных нет**; веб — как браузер | 16 % (med) | Веб-вариант = класс «браузер». Нативный — мессенджер (голосовые сообщения → короткие захваты) |
| 6 | **Контур.Толк** · СКБ Контур | **нативный deb** `ktalk` 3.2.0 в корп. репо (high); Electron — вывод по набору зависимостей electron-builder (med); deb/rpm на сайте; Ready for Astra (on-prem 0.16) ([astra.ru](https://astra.ru/ready-for-astra/compatible-software/versions/41637/), high) | `ktalk`; бинарь — вероятно `/opt/<…>/ktalk` (low) | binary `ktalk` (low); `application.name` — `Chromium` или своё, зависит от версии Electron (med) | в долях TelecomDaily отдельно не выделен; интеграция календаря с RuPost ([wiki.astralinux](https://wiki.astralinux.ru/kb/rupost-sinhronizatsiya-kalendarya-kontur-tolk-po-protokolu-caldav-326831820.html), med) | как DION. Есть веб-вход |
| 7 | **МТС Линк** (Встречи, Вебинары, Чаты) · МТС | **AppImage**, Ubuntu/РЕД ОС/Astra ([МТС Линк](https://help.mts-link.ru/article/19297), high); веб: Chrome/Edge/Яндекс.Браузер 25.6+, **Firefox не поддерживается** (high) | AppImage — имя процесса зависит от файла; фреймворк — **данных нет** | **данных нет**; веб — `Chromium`/`Yandex` | 5 % (med); NPS 37 % | ⚠ **Вебинары: участник чаще только слушает** — захвата микрофона нет → детектор «захват+вывод» звонок не увидит. Нужен режим «только слушаю» (ручной старт) |
| 8 | **SaluteJazz** (Сбер Jazz) · Сбер | **deb** `dl.salutejazz.ru/desktop/latest/jazz.deb` ([Сбер](https://developers.sber.ru/help/jazz/guide/sberjazz-application), high); совместимость с Astra SE заявлена (med) | `jazz` (по имени файла, low) | **данных нет** | 4 % (med) | веб-версия = браузер |
| 9 | **Zoom Workplace** · Zoom (США) | **нативный deb** `zoom` 6.6 в корп. репо (high); зависит от `libpulse0` (high) | `zoom`; `/opt/zoom/zoom` (low) | binary `zoom` — именно по нему ловит meeting-recorder ([config](https://github.com/ShadyF/meeting-recorder), high); `application.name` исторически «ZOOM VoiceEngine» (low, форумы) | иностранные суммарно 7 % (med); госкомпаниям Zoom ограничил доступ ([TASS](https://tass.com/economy/1274889), med) | нативный, простой. Для госсектора неприемлем, но в корп. репо ГК «Астра» есть — значит используется с внешними партнёрами |
| 10 | **Telegram Desktop** · Telegram | бинарь с сайта (не deb); на машине заказчика есть | `Telegram`; WM_CLASS `TelegramDesktop` (high) | вывод `application.name=Telegram Desktop` (high, наблюдение); захват — **данных нет** | звонки в Telegram **ограничены РКН с августа 2025**, мессенджер замедлен с 02.2026 ([Википедия](https://ru.wikipedia.org/wiki/%D0%A7%D0%B0%D1%81%D1%82%D0%B8%D1%87%D0%BD%D0%B0%D1%8F_%D0%B1%D0%BB%D0%BE%D0%BA%D0%B8%D1%80%D0%BE%D0%B2%D0%BA%D0%B0_Telegram_%D0%B8_WhatsApp_%D0%B2_%D0%A0%D0%BE%D1%81%D1%81%D0%B8%D0%B8), med) | ложные срабатывания: голосовые/видеосообщения (короткий захват). Для рабочих встреч — низкий приоритет |
| 11 | **MAX** · VK (национальный мессенджер) | deb / rpm / AppImage, **Electron** ([OpenNet](https://opennet.ru/63791), [Хабр](https://habr.com/ru/news/941502/), med) | н/д | до Electron 43 — `Chromium` (med) | госорганы массово переходят (РЕД ОС) ([АРПП](https://arppsoft.ru/news/members/20326/), med); долей по ВКС нет | в основном 1:1 и групповые звонки мессенджера; голосовые — ложные |
| 12 | **eXpress** · ООО «Анлимитед Продакшн» | десктоп Windows/macOS/Linux вкл. Astra ([iXBT/Астра](https://www.ixbt.com/live/market/item/astralinux/blog/54579.html), med) | **данных нет** | **данных нет** | реестр ПО, ФСТЭК 4 УД ([RBC](https://trends.rbc.ru/trends/social/69ccfc489a794712def798d2), med); госсектор, силовые | нативный — вероятно простой; уточнить при спайке |
| 13 | **VideoMost** · СПИРИТ | Linux-клиент, Astra/Альт/РЕД ОС; Ready for Astra №5428/2021 (8.2) ([astra.ru](https://astra.ru/about/press-center/news/videomost-i-os-astra-linux-rossiyskiy-programmnyy-stek-dlya-zashchishchennoy-videosvyazi/), high) | **данных нет** | **данных нет** | удовлетворённость 42 % (TelecomDaily, med); доля не приведена | есть веб-вариант |
| 14 | **Vinteo Desktop** · VINTEO (с 2024 в МТС Линк) | Linux: Astra, РЕД ОС, РОСА ([VINTEO wiki](https://wiki.vinteo.com/wiki/Vinteo_Desktop_v3), med); сервер 3.16 — Ready for Astra 1.7 (high) | **данных нет** | **данных нет** | региональные госорганы (Мурманская обл. — [Vinteo](https://vinteo.com/ru/about-us/vinteo-v-smi/405-importozameshchenie-vks-v-gosstrukturakh), med) | есть WebRTC-вход |
| 15 | **Р7-Команда** · «Р7» (линейка «Р7-Офис») | deb для Debian/Ubuntu/**Astra**, rpm для Альт/РЕД ([Р7](https://support.r7-office.ru/download/team/), med) | **данных нет** | **данных нет** | долей нет; встроенная запись звонков у самого клиента (med) | — |
| 16 | **Squadus** · МойОфис (НОТ) | Linux, РЕД ОС, Альт ([МойОфис](https://myoffice.ru/products/squadus/), med); ВКС на встроенном **Jitsi** (med) | **данных нет** | вероятно Electron/браузерный WebRTC (low) | долей нет | — |
| 17 | **Mattermost Calls** · Mattermost (США) | deb 5.11.2 в корп. репо и на машине (high); Electron | `mattermost-desktop`, `/opt/Mattermost/` | `Chromium`/`Chromium input` (med) | внутренний чат ГК «Астра» (вывод по корп. репо, med) | отличать от DION только по binary |
| 18 | **Jitsi Meet** (в т.ч. в составе Squadus, самохостинг) | браузер; неофиц. Electron-клиент | — | как браузер | долей нет | класс «браузер» |
| 19 | **BigBlueButton** | только браузер | — | как браузер | вузы (low, общее знание) | вебинарный режим «только слушаю» — как МТС Линк Вебинары |
| 20 | **Microsoft Teams** | родной Linux-клиент снят в 2022; только браузер/PWA или неофиц. `teams-for-linux` (Electron) (low) | `teams-for-linux` | браузер / `Chromium` | иностранные ≤7 % (med) | — |
| 21 | **Nextcloud Talk** | Electron-клиент для Linux ([GitHub](https://github.com/nextcloud/talk-desktop), med) | `nextcloud-talk-desktop` (AUR, med) | `Chromium` до Electron 43 | в РФ-корпорациях — данных нет | — |


---

## 3. Имена потоков: что проверено, а что нет

**Chromium и все браузеры на его движке** (исходник [`media/audio/pulse/pulse_util.cc`](https://chromium.googlesource.com/chromium/src/+/refs/heads/main/media/audio/pulse/pulse_util.cc), high):
- вывод: `pa_context` = имя продукта (`Chromium`, у Яндекса — `Yandex`), поток `media.name=Playback`;
- захват: **отдельный** контекст `<PRODUCT_STRING> input` → `application.name=Chromium input`, поток `media.name=RecordStream`;
- на машине заказчика оба ключа уже есть в `stream-properties` (high, наблюдение).
- **Следствие:** YouTube и звонок в Телемосте дают одинаковый поток `Playback`. Отличить можно только по наличию
  `… input` (микрофон открыт) и, по желанию, по заголовку активного окна X11 (`_NET_WM_NAME` содержит «Телемост»,
  «Контур.Толк», «SaluteJazz»…; только для **активной** вкладки — low).

**Electron** (DION, Контур.Толк, Mattermost, MAX, Nextcloud Talk…):
- до сих пор все Electron-приложения на Linux показываются как `Chromium` / `Chromium input` ([electron#7470](https://github.com/electron/electron/issues/7470), high);
- исправлено в [electron#49270](https://github.com/electron/electron/pull/49270) (слито в main 25.03.2026, метка **no-backport**, high).
  Ветка 42-x-y создана до этого (42.0.0-alpha.1 — 14.03.2026, med) → **исправление попадает только в Electron 43+** (med, вывод);
- DION 5.34.1 = Electron 42.3.3 (high, `strings`) → сейчас DION виден как **`Chromium`** — неотличим от браузера по имени,
  отличим по `application.process.binary=dion` (med) и по PID;
- **при переходе DION на Electron 43 имя сменится на `Dion`** — это конкретный пример риска R2 (§6).

**Firefox** ([bugzilla 1435614](https://bugzilla.mozilla.org/show_bug.cgi?id=1435614), med): с версии 117 `application.name=Firefox`,
`media.name` = заголовок вкладки и сайт → в Firefox **сайт звонка виден по потоку**. Для потоков WebRTC это не подтверждено (low).
Оговорка: МТС Линк Firefox не поддерживает, у остальных сервисов основной браузер — Chromium/Яндекс.

**Нативные:** Zoom — детект по binary `zoom` (так в [meeting-recorder](https://github.com/ShadyF/meeting-recorder), high), имя
«ZOOM VoiceEngine» — low. TrueConf, IVA Connect, VK WorkSpace, SaluteJazz, eXpress, VideoMost, Vinteo, Р7-Команда — **точных
`application.name`/`media.name` в открытых источниках не нашёл** (искал: документацию вендоров, ArchWiki, форумы, GitHub по
«pavucontrol»/«pulseaudio» + имя продукта). Нужен спайк: живой тестовый звонок + `pw-dump` (запускает Юрка/человек).

---

## 4. Как отличить звонок от «просто звука» — правило для детектора

1. **Ключ группировки** — `application.process.binary` (+ `application.process.id`), не `application.name`: имя меняется
   (Electron 43, ребрендинг), бинарь стабильнее. Для браузеров обе ноды (вывод и `… input`) принадлежат одному бинарю
   (`chromium`, `yandex_browser`, `firefox`).
2. **Звонок** = у одного бинаря одновременно активны захват микрофона (source-output, не монитор) и вывод (sink-input)
   ≥ 5–10 с (debounce; у meeting-recorder старт/стоп по 3 с — [default_config.json](https://github.com/ShadyF/meeting-recorder), high).
3. **Не звонок:** только вывод (видео, музыка, уведомления); только захват (диктовка, голосовое сообщение, «проверка звука»,
   вкладка с открытым микрофоном без собеседников — у Chromium `input` бывает без вывода).
4. **Исключать:** собственный PID Voice, `astra-voice*`, `handy`, `parec*`, `SimpleScreenRecorder`, `speech-dispatcher*`,
   мониторные захваты (`stream.capture.sink=true` / источник `*.monitor`).
5. **Класс «браузер»** показывать честно: «Звонок в браузере (Яндекс.Браузер)» — без угадывания сервиса; в Firefox можно
   подставить заголовок вкладки.
6. **Режим «только слушаю»** (вебинары МТС Линк, BBB, трансляции): микрофона нет → автоматически не ловится; оставить ручную кнопку.
7. Сопоставление с окном (WM_CLASS `Dion`, `TelegramDesktop`…) — дополнительный, не основной признак.

---

## 5. Рекомендуемый порядок поддержки (топ-5)

| Ранг | Что | Почему (доля) | Почему (техника) |
|---|---|---|---|
| 1 | **DION** | единственная нативная ВКС на машине заказчика; основная ВКС «Группы Астра» (~533 встреч/день); есть в корп. репо | отдельный процесс `dion` → простой детект по binary + захват/вывод; имя `Chromium` обходится ключом binary. Можно проверить на машине заказчика хоть сегодня |
| 2 | **Звонок в браузере** (Яндекс.Браузер, Chromium/Chromium-GOST, Firefox) — как класс; покрывает Телемост 9 %, VK Звонки-веб, SaluteJazz-веб, Контур.Толк-веб, МТС Линк, Jitsi, BBB | суммарно самая большая доля «сервисов без Linux-клиента»; Телемост на Linux — только браузер | имена потоков **проверены на машине** (`Chromium input`, `Yandex`, `Firefox`); сервис не определить → подпись «звонок в браузере» |
| 3 | **TrueConf** | 21 % крупного бизнеса; традиционный выбор госсектора и силовых; сборка под SE 1.8 в корп. репо | нативный процесс `TrueConf`; нужен один спайк на имена; отдельная обработка «проверки звука» |
| 4 | **IVA Connect** | лидер 33 %; сборка `.astra` в корп. репо; сервер совместим с SE 1.8.2 | нативный (GStreamer), имена не подтверждены — спайк |
| 5 | **Контур.Толк** | в корп. репо (`ktalk` 3.2.0); Ready for Astra; интеграция с RuPost | Electron → как DION, по binary |

Дальше: VK WorkSpace (16 % вместе с VK Звонками, но нативный клиент — данных по потокам нет), МТС Линк (5 %, AppImage,
вебинары без микрофона), SaluteJazz (4 %), Zoom (в корп. репо, для внешних встреч), MAX, eXpress, Telegram (звонки ограничены РКН).

---

## 6. Риски

- **R1. Браузерные звонки неотличимы от YouTube по имени потока** (Chromium-семейство, high). Смягчение: признак «есть `… input`
  + есть вывод»; ложное срабатывание — открытая вкладка с микрофоном + музыка в другой вкладке (low вероятность, low цена —
  только уведомление).
- **R2. Имена потоков меняются при обновлениях.** Конкретно: Electron 43 переименует `Chromium` → `Dion`/`ktalk`/`MAX` (med);
  Firefox менял имена в 117 (med); Chromium может сменить `PRODUCT_STRING`. Смягчение: ключ — `application.process.binary`,
  список сопоставлений — в конфиге, а не в коде; регресс-тест на «сырые» свойства из `pw-dump`.
- **R3. Имена нативных клиентов (TrueConf, IVA, VK WorkSpace, SaluteJazz) не подтверждены** — нужен спайк с живыми звонками.
- **R4. Звуковой сервер.** На машине заказчика PipeWire + pipewire-pulse, но `pipewire-pulse` в ALSE есть только в extended;
  на «чистой» ALSE может быть PulseAudio (см. `tech-meetings.md` R-A3). Свойства `application.*` есть в обоих — детект через
  libpulse работает там и там (med).
- **R5. Вебинары без микрофона** (МТС Линк Вебинары, BBB, трансляции DION) — детектор «захват+вывод» их не видит; нужна кнопка.
- **R6. AppImage/Flatpak/snap** (МТС Линк, MAX AppImage): binary = имя файла AppImage или `ld-linux`, может отличаться (low).
- **R7. Эхо/гарнитура и Bluetooth HFP** — вне этого отчёта, см. `tech-meetings.md` §1.4.
- **R8. Данные о долях.** Опрос TelecomDaily 2025 — только крупный бизнес, один ответ; эксперты ComNews считают долю
  отечественных 40–50 %, а не 90 % ([ComNews](https://www.comnews.ru/content/239928/2025-07-01/2025-w27/1008/importozameschenie-rynke-vks-esche-ne-zavershilos), med).
  **По госсектору долей нет** — искал «ВКС госорганы доля 2025», TAdviser, ComNews, anti-malware.

---

## 7. Противоречия и пробелы

- Доли: TelecomDaily «>90 % отечественных» vs эксперты ComNews «40–50 %» (оба med).
- «Телемост»: Яндекс Телемост (без Linux-клиента) ≠ «Телемост 2.0» (mytelemost.ru, deb, Astra 1.7). В поиске смешиваются.
- TrueConf: на сайте вендора Astra в списке дистрибутивов нет (med), но в корп. репо ГК «Астра» лежит `trueconf_client_astralinux_se18` (high).
- В сохранённых потоках нет ни одного «Dion» — либо DION-звонков после перехода на PipeWire не было, либо (вероятнее, med) он
  записан как `Chromium input`. Проверить можно только живым звонком.
- Пробелы («данных нет»): `application.name` у TrueConf, IVA Connect, VK WorkSpace, SaluteJazz, eXpress, VideoMost, Vinteo,
  Р7-Команда; фреймворк SaluteJazz и МТС Линк; доли в госсекторе.

## Источники

- Локально (high): `/var/lib/dpkg/status`; `/usr/share/applications/{dion,chromium-gost,yandex-browser,firefox,mattermost-desktop,psi-plus}.desktop`;
  `~/.local/share/applications/org.telegram.desktop*.desktop`; `/var/lib/apt/lists/artifactory.astralinux.ru…gca-service-repo…Packages`;
  `~/.local/state/wireplumber/stream-properties`; `~/.config/pulse/*-stream-volumes.tdb`; `/opt/Dion/dion` (Electron/42.3.3).
- Рынок: [Хабр/TelecomDaily](https://habr.com/ru/news/923634/), [ComNews](https://www.comnews.ru/content/239928/2025-07-01/2025-w27/1008/importozameschenie-rynke-vks-esche-ne-zavershilos),
  [Forbes](https://www.forbes.ru/tekhnologii/540697-rossijskie-servisy-videokoferencsvazi-kontroliruut-bolee-90-korporativnogo-rynka),
  [anti-malware.ru](https://www.anti-malware.ru/analytics/Market_Analysis/Russian-video-conferencing-2025), [Коммерсантъ](https://www.kommersant.ru/doc/4955531).
- Клиенты: ссылки в таблице §2.
- Техника: [Chromium pulse_util.cc](https://chromium.googlesource.com/chromium/src/+/refs/heads/main/media/audio/pulse/pulse_util.cc),
  [electron#49270](https://github.com/electron/electron/pull/49270), [electron#7470](https://github.com/electron/electron/issues/7470),
  [Mozilla 1435614](https://bugzilla.mozilla.org/show_bug.cgi?id=1435614), [meeting-recorder](https://github.com/ShadyF/meeting-recorder).
