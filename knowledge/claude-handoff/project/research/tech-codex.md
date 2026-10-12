# Технический ресёрч #2 (Codex / GPT-5.6 Sol) — astra-voice

> **Движок:** `codex exec -m gpt-5.6-sol -c model_reasoning_effort=xhigh -s read-only`, codex-cli 0.150.0-alpha.8.
> Три независимых запуска: блоки 1–2, блоки 3–4, блок 5. Дата: 2026-09-07.
> **Это второе, независимое мнение.** ⚠️ У Sol документированный reward-hacking — ссылки подлежат сверке
> при своде. Что я (Researcher #2) уже проверил лично — отмечено ниже в разделе «Сверка».

## Легенда уверенности
- `H` / high — подтверждено первичным источником, ссылка рабочая.
- `M` / med — обоснованная экстраполяция или косвенный источник.
- `L` / low — источник не найден / только утверждение модели.
- `ИСТ` — цифра из источника; `ОЦ` — оценка модели; `П` — наш собственный замер на машине заказчика.

## Сверка ссылок (сделана мной через HF Hub API, не со слов Sol)
| Утверждение Sol | Проверка | Итог |
|---|---|---|
| `csukuangfj/sherpa-onnx-nemo-transducer-punct-giga-am-v3-russian-2025-12-16` существует, ~232 МБ | Hub API: encoder.int8.onnx 224 570 820 Б + decoder.onnx 4 600 132 Б + joiner.onnx 2 712 896 Б + tokens.txt 13 354 Б | ✅ подтверждено (это ровно та модель, что стоит в handy-gigaam) |
| `csukuangfj/sherpa-onnx-nemo-ctc-giga-am-v3-russian-2025-12-16` | Hub API: репо есть, обновлён 16.12.2025 | ✅ |
| `Smirnov75/GigaAM-v3-sherpa-onnx` — все 4 варианта, fp32 и int8 | Hub API: ctc 885 264 480 / int8 319 183 165; e2e_ctc 885 950 432 / 319 869 121; rnnt_encoder 885 084 896 / 318 995 995; e2e_rnnt_encoder 885 084 898 / 318 995 997. Лицензия mit, ru+en | ✅ размеры сходятся с точностью до мегабайта |
| `ai-sage/GigaAM-v3` — основная публикация весов | Hub API: 1.1M загрузок, license mit, обновлён 19.11.2025 | ✅ |
| `istupakov/gigaam-v3-onnx` — экспорт под onnx-asr, не sherpa | Hub API: теги `onnx-asr`, base_model ai-sage/GigaAM-v3, license mit | ✅ |
| `t-tech/T-one` — 71,7M, Apache-2.0 | Hub API: параметры 71.7M, license apache-2.0, теги conformer/streaming | ✅ |
| `Alexxerm/gigaam-v3-e2e-rnnt-sherpa-onnx` — источник замера 0,029 RTF | Hub API: репо есть, library sherpa-onnx, но **21 загрузка** — это частный экспорт одного человека, замер на Galaxy S25 | ⚠️ существует, но вес источника низкий |

**Не проверял лично** (Юрке — при своде): ссылки на wiki.astralinux.ru (PDF руководства КСЗ), docs.astralinux.ru,
полки GitHub-репозиториев, цифры WER из `GigaAM/evaluation.md`, бенчмарки faster-whisper, `transcribe.cpp`.

## Эталон из соседнего проекта (наш факт, не Sol)
`~/Документы/handy-gigaam`: закреплённая ревизия `a6039be7cee829a9044a69ac0ebaf1c191217c97`,
SHA256 четырёх файлов в `model/SHA256SUMS`; хоткей — `rdev` (форк rustdesk-org), вставка — `enigo 0.6.1`
(clipboard + Ctrl+V, настраиваемая задержка, альтернативы Ctrl+Shift+V / Shift+Insert), звук — `cpal 0.16`,
VAD — Silero через `vad-rs`, трей — `tauri tray-icon`. Зависимости .deb: `libgtk-layer-shell0, xdotool,
libasound2, libayatana-appindicator3-1, libwebkit2gtk-4.1-0, libgtk-3-0`, Installed-Size 47 МБ.

---

## Расхождения внутри самого вывода Sol (важно при своде)
1. **sherpa-onnx × GigaAM v3.** Запуск блока 1 нашёл четыре готовых репо `csukuangfj/sherpa-onnx-nemo-*-giga-am-v3-russian-2025-12-16` (я подтвердил их через Hub API). Запуск блока 5 независимо написал, что «в официальном каталоге sherpa-onnx страницы GigaAM v3 нет» и сослался на community-конверсии `pantinor/gigaam-v3`. **Оба верны и не противоречат друг другу:** `csukuangfj` — это Fangjun Kuang, автор sherpa-onnx, то есть репо квази-официальные, но отдельной страницы в docs-каталоге они пока не получили. Практический вывод: v3 работает как `OfflineRecognizer` + `model_type="nemo_transducer"`, но пресет держим у себя с закреплённым commit и SHA256 (ровно как уже сделано в handy-gigaam).
2. **Пунктуация.** Блок 1: пунктуация есть только у e2e-вариантов (`*-punct-*`), у обычных CTC/RNN-T её нет. Блок 5: отдельной русской punctuation-модели в каталоге sherpa нет. Значит вариант «обычный RNN-T + отдельная пунктуация» отпадает — берём e2e RNN-T (это и есть наша текущая модель).
3. **Автозапуск.** Блок 4 рекомендует XDG autostart, а не `systemd --user`, и честно пишет, что «активирует ли Fly-сессия Astra 1.8.5 `graphical-session.target`» подтвердить не удалось. Это надо проверить руками на машине: `systemctl --user is-active graphical-session.target`.
4. **ЗПС.** Ключевой вывод (высокая уверенность): при `DIGSIG_ELF_MODE=1` неподписанный сторонний ELF/`.so` не запускается, и **самообновление невозможно без подписи доверенным ключом**. Это бьёт по Python-стеку сильнее, чем по Rust/C++ (каждое native-колесо = свои `.so`). Проверить формулировки по первоисточнику — PDF руководства КСЗ, а не по пересказу Sol.
5. **Оценки vs замеры.** Все RTF/ОЗУ для Core Ultra 7 255U у Sol — оценки (`ОЦ`), кроме нашего собственного 0,0217 (`П`). Внешний замер 0,029 взят с телефона Galaxy S25 из репо с 21 загрузкой — вес источника низкий.

## Чего Sol НЕ нашёл (честные пробелы)
- Российского зеркала GigaAM v3 с регулярным обновлением — нет; рекомендация: класть базовую модель в пакет либо держать своё HTTPS/S3-зеркало.
- Подтверждения постоянной блокировки Hugging Face / GitHub API из РФ — нет; но и гарантий доступности нет.
- Официального документа Astra про сочетание AppImage/venv с ЗПС — нет (вывод логический).
- Наличия `snapd` именно в подключённых репозиториях 1.8.5 — подтвердить удалённо не смог.
- Версии libadwaita и пакетов `qml6-module-*` именно в Astra 1.8.5 — проверять `apt-cache policy` на машине.
- Воспроизводимого RU WER для Whisper large-v3-turbo — не найден.

---

# Блоки 1–2 — модели и обновление модели

## Краткий вывод

Для данной машины я бы оставил **GigaAM v3 RNN‑T int8** основным профилем: ваш замер `130 мс / 6 с = RTF 0,0217` существенно лучше ожидаемой скорости альтернатив. Если нужна встроенная пунктуация — **GigaAM v3 e2e RNN‑T int8**; универсальные запасные варианты — **Whisper small int8/Q5** и **Whisper large‑v3‑turbo int8/Q5**. Parakeet интересен качеством, но тяжелее; Voxtral для CPU нерационален.

Обозначения: **ИСТ** — измерение/число из источника; **П** — ваш замер; **ОЦ** — моя оценка для Core Ultra 7 255U. `H/M/L` — высокая/средняя/низкая уверенность.

# Блок 1. Каталог моделей

## GigaAM v3 и v2

У v3 есть `v3_ssl`, `v3_ctc`, `v3_rnnt`, `v3_e2e_ctc`, `v3_e2e_rnnt`; e2e-модели одновременно делают ASR, пунктуацию и нормализацию текста. Обучающие объёмы выросли с `50k/2k ч` у v2 до `700k/4k ч` у v3; авторы сообщают до 30% улучшения на новых внутренних доменах, но близкие к v2 результаты на публичных наборах. [README GigaAM](https://github.com/salute-developers/GigaAM#readme) `[ИСТ,H]`

v2 принесла примерно −15% WER для CTC и −12% для RNN‑T относительно v1, а также MIT-лицензию и официальный ONNX-экспорт. У v3 также именно **MIT**, что подтверждается актуальным LICENSE, а не старая non-commercial лицензия раннего GigaAM v1. [README](https://github.com/salute-developers/GigaAM#readme), [LICENSE](https://github.com/salute-developers/GigaAM/blob/main/LICENSE) `[ИСТ,H]`

Реально лежащие на HF и непосредственно подготовленные для sherpa-onnx репозитории:

- `csukuangfj/sherpa-onnx-nemo-ctc-giga-am-v3-russian-2025-12-16` — CTC int8, модель 225 MB. [Файлы](https://huggingface.co/csukuangfj/sherpa-onnx-nemo-ctc-giga-am-v3-russian-2025-12-16/tree/main) `[ИСТ,H]`
- `csukuangfj/sherpa-onnx-nemo-transducer-giga-am-v3-russian-2025-12-16` — RNN‑T int8, суммарно около 230 MB. [Файлы](https://huggingface.co/csukuangfj/sherpa-onnx-nemo-transducer-giga-am-v3-russian-2025-12-16/tree/main) `[ИСТ,H]`
- `csukuangfj/sherpa-onnx-nemo-ctc-punct-giga-am-v3-russian-2025-12-16` — e2e CTC int8, 225 MB. [Файлы](https://huggingface.co/csukuangfj/sherpa-onnx-nemo-ctc-punct-giga-am-v3-russian-2025-12-16/tree/main) `[ИСТ,H]`
- `csukuangfj/sherpa-onnx-nemo-transducer-punct-giga-am-v3-russian-2025-12-16` — e2e RNN‑T int8, около 232 MB. [Файлы](https://huggingface.co/csukuangfj/sherpa-onnx-nemo-transducer-punct-giga-am-v3-russian-2025-12-16/tree/main) `[ИСТ,H]`
- `Smirnov75/GigaAM-v3-sherpa-onnx` — все четыре варианта, **fp32 и int8**, уже с sherpa-метаданными. CTC/e2e CTC: `885–886/319–320 MB`; RNN‑T: примерно `890–892/324–326 MB`. [Файлы и описание](https://huggingface.co/Smirnov75/GigaAM-v3-sherpa-onnx/tree/main) `[ИСТ,H]`

`istupakov/gigaam-v3-onnx` также содержит fp32/int8, но экспорт предназначен для `onnx-asr`: без правки метаданных и графов не следует считать его drop-in-моделью sherpa. [Репозиторий](https://huggingface.co/istupakov/gigaam-v3-onnx/tree/main) `[ИСТ,M]`

| модель/вариант | рантайм | размер на диске | ОЗУ в работе | скорость RTF CPU | качество ru | пунктуация/капитализация | лицензия | ссылка | уверенность |
|---|---|---:|---:|---:|---|---|---|---|---|
| GigaAM v3 CTC int8 | sherpa-onnx/ORT | 225–320 MB ИСТ | 0,45–0,8 GB ОЦ | 0,02–0,06 ОЦ | средний WER 9,1%; Golos Crowd 2,8%; Ru Libri 4,7% ИСТ | нет/нет | MIT | [оценка](https://github.com/salute-developers/GigaAM/blob/main/evaluation.md), [ONNX](https://huggingface.co/csukuangfj/sherpa-onnx-nemo-ctc-giga-am-v3-russian-2025-12-16/tree/main) | H/M |
| GigaAM v3 RNN‑T int8 | sherpa-onnx/ORT | 230–326 MB ИСТ; у вас 222 MB П | 0,45–0,8 GB ОЦ | **0,0217 П**; 0,02–0,06 ОЦ | средний WER 8,3%; Golos Crowd 2,4%; Ru Libri 4,4% ИСТ | нет/нет | MIT | [оценка](https://github.com/salute-developers/GigaAM/blob/main/evaluation.md), [ONNX](https://huggingface.co/csukuangfj/sherpa-onnx-nemo-transducer-giga-am-v3-russian-2025-12-16/tree/main) | H |
| GigaAM v3 e2e CTC int8 | sherpa-onnx/ORT | 225–320 MB ИСТ | 0,45–0,85 GB ОЦ | 0,02–0,07 ОЦ | нормализованный средний WER 12,0%; punct F1: запятая 83,7, точка 86,7, вопрос 78,6 ИСТ | да/да | MIT | [оценка](https://github.com/salute-developers/GigaAM/blob/main/evaluation.md), [ONNX](https://huggingface.co/csukuangfj/sherpa-onnx-nemo-ctc-punct-giga-am-v3-russian-2025-12-16/tree/main) | H/M |
| GigaAM v3 e2e RNN‑T int8 | sherpa-onnx/ORT | 226–326 MB ИСТ | 0,45 GB ИСТ¹; 0,45–0,9 ОЦ | 0,029 ИСТ¹; 0,02–0,07 ОЦ | средний WER 11,2%; punct F1 84,5/86,7/79,8 ИСТ | да/да | MIT | [бенчмарк/ONNX](https://huggingface.co/Alexxerm/gigaam-v3-e2e-rnnt-sherpa-onnx), [оценка](https://github.com/salute-developers/GigaAM/blob/main/evaluation.md) | M |
| GigaAM v3 fp32, все ASR-варианты | sherpa-onnx/ORT | 885–892 MB ИСТ | 1,1–1,8 GB ОЦ | 0,05–0,15 ОЦ | как соответствующий v3, без потерь int8-квантизации | зависит от варианта | MIT | [Smirnov75](https://huggingface.co/Smirnov75/GigaAM-v3-sherpa-onnx/tree/main) | M |
| GigaAM v2 CTC int8 | sherpa-onnx/ORT | 236 MB ИСТ | 0,5–0,9 GB ОЦ | 0,02–0,07 ОЦ | средний WER 11,1%; Natural 10,8%; Disordered 28,0% ИСТ | нет/нет | MIT upstream | [ONNX](https://huggingface.co/csukuangfj/sherpa-onnx-nemo-ctc-giga-am-v2-russian-2025-04-19/tree/main), [оценка](https://github.com/salute-developers/GigaAM/blob/main/evaluation.md) | H/M |

¹ Samsung Galaxy S25 Ultra CPU, 2 потока, сегменты 30 с — полезный внешний замер, но не прогноз для вашего Intel.

Есть также CPU-native `transcribe.cpp`: GigaAM v3 e2e RNN‑T `F32/F16/Q8_0/Q6_K/Q5_K_M/Q4_K_M` занимает `849/431/261/217/197/175 MB`, WER на FLEURS-ru `5,35–5,42%`. [Документация transcribe.cpp](https://github.com/handy-computer/transcribe.cpp/blob/main/docs/models/gigaam-v3-e2e-rnnt.md) `[ИСТ,H]`

## Whisper

Параметры OpenAI: tiny 39M, base 74M, small 244M, medium 769M, large 1550M, turbo 809M; turbo — ускоренная версия large-v3 с небольшим общим ухудшением качества. [OpenAI Whisper](https://github.com/openai/whisper#available-models-and-languages) `[ИСТ,H]`

В официальном `whisper.cpp`-репозитории есть готовые GGML-модели; поддерживаются Q4_0/Q4_1/Q5_0/Q5_1/Q8_0, хотя готовые файлы чаще представлены Q5_1 для малых моделей и Q5_0 для medium/large. [Модели](https://huggingface.co/ggerganov/whisper.cpp/tree/main), [квантизация](https://github.com/ggml-org/whisper.cpp/discussions/838) `[ИСТ,H]`

| модель/вариант | рантайм | размер на диске | ОЗУ в работе | скорость RTF CPU | качество ru | пунктуация/капитализация | лицензия | ссылка | уверенность |
|---|---|---:|---:|---:|---|---|---|---|---|
| Whisper tiny Q5/Q8 | whisper.cpp | 32/44 MB ИСТ | 0,15–0,3 GB ОЦ | 0,03–0,08 ОЦ | FLEURS-ru WER 31,1% ИСТ | да/да | MIT | [paper](https://cdn.openai.com/papers/whisper.pdf), [файлы](https://huggingface.co/ggerganov/whisper.cpp/tree/main) | H/M |
| Whisper base Q5/Q8 | whisper.cpp | 60/82 MB ИСТ | 0,25–0,45 GB ОЦ | 0,05–0,12 ОЦ | FLEURS-ru 20,5% ИСТ | да/да | MIT | [paper](https://cdn.openai.com/papers/whisper.pdf), [файлы](https://huggingface.co/ggerganov/whisper.cpp/tree/main) | H/M |
| Whisper small Q5/Q8 или CT2 int8 | whisper.cpp / faster-whisper | 190/264 MB ИСТ; CT2 зависит от упаковки | 0,6–1,5 GB ИСТ/ОЦ | 0,12–0,25 ОЦ | FLEURS-ru 11,4% ИСТ | да/да | MIT | [paper](https://cdn.openai.com/papers/whisper.pdf), [CPU benchmark](https://github.com/SYSTRAN/faster-whisper#benchmark) | M |
| Whisper medium Q5/Q8 | whisper.cpp / CT2 | 539/823 MB ИСТ | 1,3–2,5 GB ОЦ | 0,30–0,65 ОЦ | FLEURS-ru 7,2% ИСТ | да/да | MIT | [paper](https://cdn.openai.com/papers/whisper.pdf), [файлы](https://huggingface.co/ggerganov/whisper.cpp/tree/main) | M |
| Whisper large-v3 Q5/fp16 | whisper.cpp / CT2 | 1,08/3,1 GB ИСТ | 2,8–5 GB ОЦ | 0,65–1,3 ОЦ | FLEURS-ru 3,1% в оценке GigaAM ИСТ | да/да | MIT | [файлы](https://huggingface.co/ggerganov/whisper.cpp/tree/main), [RU benchmark](https://github.com/salute-developers/GigaAM/blob/main/evaluation.md) | M |
| Whisper large-v3-turbo Q5/Q8 | whisper.cpp / CT2 | 574/874 MB ИСТ | 1,5–3 GB ОЦ | 0,18–0,40 ОЦ | сопоставимо с large-v3 в среднем; отдельного воспроизводимого RU WER не найдено | да/да | MIT | [OpenAI](https://github.com/openai/whisper#available-models-and-languages), [файлы](https://huggingface.co/ggerganov/whisper.cpp/tree/main) | M |
| Distil-Whisper large-v3 | CT2 | около 1,5 GB fp16 | 1,5–3 GB ОЦ | 0,2–0,5 ОЦ | **English-only: для русского не подходит** | для английского | MIT | [карточка](https://huggingface.co/distil-whisper/distil-large-v3) | H |
| whisper-small-ru-pruned-ft | Transformers; конвертация в CT2/ggml | 821 MB fp32 ИСТ | 0,8–1,6 GB ОЦ | 0,10–0,25 после int8 ОЦ | CV15 WER 15,71 raw / 10,92 normalized, self-report | да, но слабые цифры/символы | Apache-2.0 | [HF](https://huggingface.co/waveletdeboshir/whisper-small-ru-pruned-ft) | M |
| whisper-small-ru-v2 | Transformers; конвертация | 967 MB fp32 ИСТ | 0,9–1,7 GB ОЦ | 0,12–0,27 после int8 ОЦ | CV15 WER 12,675%, self-report | да/да | Apache-2.0 | [HF](https://huggingface.co/artyomboyko/whisper-small-ru-v2) | M |

Опорный внешний CPU-тест: faster-whisper small int8 обработал 13 минут за 102 с (`RTF≈0,131`, RAM 1477 MB) на i7‑12700K/8 потоках; whisper.cpp fp32 — 125 с и 1049 MB. Все цифры для 255U выше — оценки с поправкой на мобильный теплопакет и интерактивный batch=1. [Benchmark](https://github.com/SYSTRAN/faster-whisper#benchmark) `[ИСТ/ОЦ,M]`

Практически: tiny/base быстры, но заметно проигрывают GigaAM по русскому; small уже пригоден; medium допустим при терпимой задержке; turbo — лучший Whisper-компромисс; полный large-v3 может уходить за real-time при длительной CPU-нагрузке.

## Другие русские offline-модели

| модель/вариант | рантайм | размер на диске | ОЗУ в работе | скорость RTF CPU | качество ru | пунктуация/капитализация | лицензия | ссылка | уверенность |
|---|---|---:|---:|---:|---|---|---|---|---|
| Vosk small-ru-0.22 | Vosk/Kaldi | 45 MB ИСТ | около 0,3 GB ИСТ | 0,01–0,05 ОЦ | Golos Crowd 11,79%; OpenSTT books 22,71%, YouTube 31,97% ИСТ | нет/нет | Apache-2.0 | [Vosk models](https://alphacephei.com/vosk/models) | H/M |
| Vosk ru-0.42 large | Vosk/Kaldi | 1,8 GB ИСТ | до нескольких GB; семейство big до 16 GB ИСТ | 0,05–0,2 ОЦ | Golos 4,4%; own audiobooks 4,5%; OpenSTT books 11,1% ИСТ | нет; отдельный recase/punct 1,6 GB | Apache-2.0 | [Vosk models](https://alphacephei.com/vosk/models) | H/M |
| T-one 71,7M | ONNX/CTC, optional KenLM | ONNX 144 MB; LM 5,46 GB ИСТ | 0,4–0,8 GB без LM; 6–9 GB с LM ОЦ | 0,03–0,1 / 0,05–0,2 с LM ОЦ | callcenter 8,63%; CV19 5,32%; OpenSTT original 20,27%, relabel 7,94% ИСТ | в базовом pipeline нет | Apache-2.0 | [HF](https://huggingface.co/t-tech/T-one), [GitHub](https://github.com/voicekit-team/T-one) | H/M |
| Silero STT RU | старый Silero STT | актуальной standalone CE-модели нет | — | — | — | — | STT CE non-commercial | [статус](https://github.com/snakers4/silero-models/discussions/123), [лицензирование](https://github.com/snakers4/silero-models/wiki/licensing-and-tiers) | M |
| NeMo ru Conformer CTC large | NeMo/PyTorch | около 0,49 GB ИСТ | 1,5–3 GB ОЦ | 0,1–0,3 ОЦ | CV10 test 4,28%; Golos Crowd 2,77%; farfield 7,15%; RuLS 13,60% ИСТ | нет/нет | CC-BY-4.0 | [HF](https://huggingface.co/nvidia/stt_ru_conformer_ctc_large) | H/M |
| wav2vec2-large-xlsr-53-russian | Transformers/CTC | около 1,3 GB ИСТ | 2–4 GB ОЦ | 0,1–0,3 ОЦ | CV6 WER 13,3% greedy / 9,57% LM; robust 40,22/33,61% ИСТ | нет/нет | Apache-2.0 | [HF](https://huggingface.co/jonatasgrosman/wav2vec2-large-xlsr-53-russian) | H/M |
| Parakeet TDT 0.6B v3 int8 | sherpa-onnx | 671 MB ИСТ | 1,0–1,8 GB ОЦ | около 0,03 ИСТ²; 0,03–0,10 ОЦ | FLEURS-ru 5,51%; CoVoST-ru 3,00% ИСТ | да/да | CC-BY-4.0 | [NVIDIA](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3), [sherpa int8](https://huggingface.co/csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8/tree/main) | H/M |
| Voxtral Mini 4B Realtime | Transformers/vLLM | около 8–9 GB BF16 ОЦ | 12–20 GB ОЦ | достоверного CPU-теста нет; вероятно хуже real-time | FLEURS-ru 6,02% при 480 мс / 5,41% при 2400 мс ИСТ | да/да | Apache-2.0 | [HF](https://huggingface.co/mistralai/Voxtral-Mini-4B-Realtime-2602) | H для WER, L для CPU |

² Публичный ONNX-тест с неуказанным современным x64 CPU; переносимость результата ограничена. [ONNX-ASR benchmarks](https://github.com/istupakov/onnx-asr/blob/main/docs/benchmarks.md) `[ИСТ,L]`

## Как честно рисовать полоски

«Качество» следует считать только на одном фиксированном корпусе вашей предметной области: минимум 500–1000 реальных PTT-фраз. Основная метрика — нормализованный WER; отдельно показывать punctuation F1, capitalization accuracy и ошибки на числах. Не смешивать WER с FLEURS, Golos и внутренних call-center-наборов в одну шкалу.

Практичная формула: `Q = 100 × clamp((30 − WER)/(30 − 5), 0, 1)`. Если оформление текста важно: `Qtotal = 0,8×Qwer + 0,2×F1punct`. Пороги 5/30% должны быть видимы пользователю как продуктовые настройки. `[ОЦ,H]`

«Скорость»: измерять прогретую модель на целевой машине, фиксированных 6-секундных фразах, batch=1, одинаковом числе потоков; брать `p95(end-to-end latency / audio duration)`. Шкала: `S = 100 × clamp(ln(1,0/RTF)/ln(1,0/0,02),0,1)`. Холодную загрузку показывать отдельно. Ваш RTF `0,0217` почти достигает верхнего порога. `[ОЦ,H]`

«ОЗУ» — максимальный дополнительный PSS/RSS процесса после загрузки и warm-up; фиксировать пик `/usr/bin/time -v` и PSS из `/proc/PID/smaps_rollup`. Для ONNX учитывать веса, arena allocator, активации и возможные деквантизованные копии; для ggml — mmap-страницы плюс compute/context buffers; для CT2 — веса, workspace, KV/cache и beam. Размер файла нельзя выдавать за RAM. Ориентир на batch=1: ONNX int8 часто `1,5–3×` размера весов, fp32 `1,2–2×`; это только предварительная оценка, которую заменяет замер. [Официальная RAM-таблица whisper.cpp](https://github.com/ggml-org/whisper.cpp#memory-usage) `[ОЦ,M]`

# Блок 2. Проверка обновлений

## Hugging Face Hub API

Текущий commit репозитория:

```text
GET https://huggingface.co/api/models/{repo_id}
GET https://huggingface.co/api/models/{repo_id}/revision/{branch_or_sha}
```

Сравнивать нужно JSON-поле `sha` с сохранённым локально `revision_sha`. Полезно хранить manifest: `repo_id`, точный commit, имена файлов, размеры и SHA-256. [HfApi model_info](https://huggingface.co/docs/huggingface_hub/package_reference/hf_api#huggingface_hub.HfApi.model_info) `[H]`

Полный список файлов с метаданными:

```text
GET https://huggingface.co/api/models/{repo_id}?blobs=true
GET https://huggingface.co/api/models/{repo_id}/revision/{revision}?blobs=true
```

Нужные поля: `siblings[].rfilename`, `size`, `blobId`, `lfs.size`, `lfs.sha256`, `lfs.pointerSize`. В актуальном model-info это **`lfs.sha256`**, не `lfs.oid`; `oid sha256:...` — строка внутри Git-LFS pointer, а `oid` встречается также в административном LFS API. [RepoSibling/LFS metadata](https://huggingface.co/docs/huggingface_hub/package_reference/hf_api) `[H]`

Проверка одного файла без тела:

```text
HEAD https://huggingface.co/{repo_id}/resolve/main/{filename}
HEAD https://huggingface.co/{repo_id}/resolve/{commit_sha}/{filename}
```

До перехода на CDN читать `X-Repo-Commit`. `ETag` равен Git blob SHA-1 для обычного файла либо SHA-256 LFS-объекта; при редиректе могут использоваться `X-Linked-ETag` и `X-Linked-Size`. Поэтому версию модели определять по `X-Repo-Commit`, а целостность LFS-файла — по `lfs.sha256`. [HTTP constants](https://github.com/huggingface/huggingface_hub/blob/main/src/huggingface_hub/constants.py), [cache reference](https://huggingface.co/docs/huggingface_hub/en/guides/manage-cache) `[H]`

Публичные репозитории доступны без токена; токен нужен для private/gated и даёт отдельную квоту. Документированные лимиты, введённые в сентябре 2025: anonymous/IP — 500 Hub API, 3000 resolver и 100 page-запросов за фиксированные 5 минут; free authenticated — 1000/5000/200. Значения могут меняться: читать `RateLimit` и `RateLimit-Policy`, на `429` применять backoff. [HF rate limits](https://huggingface.co/docs/hub/en/rate-limits) `[H]`

## Поиск близких моделей

Точный запрос по автору:

```text
GET https://huggingface.co/api/models?search=GigaAM&author=salute-developers&filter=automatic-speech-recognition&sort=lastModified&direction=-1&limit=50
```

Но он не найдёт основную публикацию `ai-sage/GigaAM-v3` и сторонние ONNX-конверсии. Поэтому безопаснее иметь allowlist авторов `ai-sage`, `csukuangfj`, `Smirnov75`, дополненный поиском по `GigaAM`, `russian`, `onnx`, `automatic-speech-recognition`. Результат поиска только предлагать пользователю, не устанавливать автоматически: имя модели не гарантирует совместимый tokenizer/граф/лицензию. [HF search guide](https://huggingface.co/docs/huggingface_hub/guides/search), [upstream weights](https://huggingface.co/ai-sage/GigaAM-v3) `[ИСТ/ОЦ,H]`

GitHub:

```text
GET https://api.github.com/repos/salute-developers/GigaAM/releases
GET https://api.github.com/repos/salute-developers/GigaAM/tags
GET https://api.github.com/repos/salute-developers/GigaAM/commits/main
```

На момент проверки у GigaAM нет оформленных Releases и tags, поэтому GitHub годится лишь как сигнал об изменениях кода; ревизию весов нужно брать с HF. [Releases](https://github.com/salute-developers/GigaAM/releases), [Tags](https://github.com/salute-developers/GigaAM/tags), [GitHub API](https://docs.github.com/en/rest/releases/releases) `[H]`

## Поведение при запуске

Окно приложения должно появляться до сетевой проверки; проверка идёт в background worker. Рекомендуемые границы: connect timeout 1–2 с, общий 3–5 с, без повторов при старте, кеш результата на 24 часа и кнопка ручной проверки. Ошибка сети не меняет статус локальной модели и не блокирует ввод. `[ОЦ,H]`

Учитывать:

- `HF_HUB_OFFLINE=1`: полностью пропустить сеть. [Offline mode](https://huggingface.co/docs/huggingface_hub/package_reference/environment_variables#hfhuboffline) `[H]`
- `HF_HUB_ETAG_TIMEOUT` и `HF_HUB_DOWNLOAD_TIMEOUT`, штатно по 10 с; приложению разумно использовать меньший timeout для startup-check. [Environment variables](https://huggingface.co/docs/huggingface_hub/package_reference/environment_variables) `[H]`
- `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`; корпоративный CA через `SSL_CERT_FILE`/`SSL_CERT_DIR`. Не отключать TLS verification. [HTTPX environment variables](https://www.python-httpx.org/environment_variables/) `[H]`

## Скачивание, resume и атомарная замена

Алгоритм:

1. Получить commit и manifest; закрепить URL на `/resolve/{commit_sha}/...`, а не `main`.
2. Скачивать в `<file>.part`. При существующем фрагменте `N` запросить `Range: bytes=N-`.
3. Продолжать только при `206` и корректном `Content-Range`; при `200` начать файл заново.
4. Проверить точный размер и SHA-256 из `lfs.sha256`. Для non-LFS нужен собственный доверенный SHA-256 manifest: Git ETag — не обычный SHA-256 файла.
5. Выполнить `fsync`, затем `os.replace(part, final)` на той же файловой системе; manifest заменить последним. [`os.replace`](https://docs.python.org/3/library/os.html#os.replace) `[H]`
6. Для RNN‑T безопаснее версия-каталог: `<sha>.partial/` → проверка encoder/decoder/joiner/tokens → `<sha>/` → атомарная смена маленького файла `current.json`. Активный процесс продолжает использовать старую загруженную модель до следующей перезагрузки. `[ОЦ,H]`

`huggingface_hub` сам возобновляет загрузки, когда сервер это позволяет; параметр `resume_download` устарел и игнорируется. [HF validators](https://github.com/huggingface/huggingface_hub/blob/main/src/huggingface_hub/utils/_validators.py) `[H]`

## Зеркала и доступность из РФ

Надёжного публичного российского зеркала с точной, регулярно обновляемой копией GigaAM v3 я не нашёл. Доступность HF может зависеть от провайдера, мобильной сети, зарубежного CDN и корпоративной фильтрации; доказательств постоянной общероссийской блокировки именно Hugging Face нет. `[ИСТ,M]`

`hf-mirror.com` — неофициальное китайское зеркало с `HF_ENDPOINT`, но отмечались редиректы обратно на основной Hub и отсутствие `X-Repo-Commit`, ломающие `snapshot_download`; его нельзя делать безусловным доверенным fallback. [Quickstart](https://github.com/Mirrors-Project/hf-mirror/blob/main/docs/en/quickstart.md), [актуальная проблема](https://github.com/huggingface/huggingface_hub/issues/4637) `[M]`

Для промышленной утилиты надёжнее поставлять базовую модель вместе с пакетом либо держать собственное HTTPS/S3-зеркало: подписанный manifest, фиксированный commit, SHA-256 каждого файла и явное отображение источника пользователю. ModelScope не является прозрачной заменой `HF_ENDPOINT`; наличие там одноимённой модели требует отдельной проверки файлов, ревизии и лицензии. `[ОЦ,H]`

---

# Результат исследования

Актуальность проверки: **07.09.2026**.  
Метки: **H** — прямо подтверждено первичным/официальным источником; **M** — обоснованная экстраполяция на Astra 1.8.5; **L** — подтверждение неполное или источник не найден.

## Краткая рекомендация

- Основной канал для корпоративных установок: **подписанный APT-репозиторий + `.deb` + маленький привилегированный update-helper через polkit**. APT-подпись подтверждает происхождение пакета, но не заменяет подпись ЗПС. **[H]** [APT secure](https://manpages.debian.org/bookworm/apt/apt-secure.8.en.html), [руководство КСЗ Astra 1.8, §13.1](https://wiki.astralinux.ru/download/attachments/371790129/3.8_nocert_Ruk_KSZ_1.pdf?api=v2)
- Для GitHub Releases допустимо скачивать `.deb`, но helper должен отдельно проверить подписанный вами манифест/артефакт и затем выполнить одну операцию `apt-get install`. Один запуск helper означает одно окно аутентификации. **[H/M]** [pkexec](https://polkit.pages.freedesktop.org/polkit/pkexec.1.html), [polkit actions](https://polkit.pages.freedesktop.org/polkit/polkit.8.html)
- Пользовательская установка в `~/.local` удобна только для обычных машин. **Она не обходит ЗПС** и потому не должна считаться корпоративно совместимым каналом без процедуры подписания. **[H]** [руководство КСЗ Astra 1.8](https://wiki.astralinux.ru/download/attachments/371790129/3.8_nocert_Ruk_KSZ_1.pdf?api=v2)
- Для автозапуска GUI с треем рекомендую **XDG Autostart**, а не `systemd --user`: Fly и KDE его документированно понимают, и приложение получает корректное окружение X11. **[H/M]** [Astra: автозапуск во Fly](https://wiki.astralinux.ru/spaces/flyingpdf/pdfpageexport.action?pageId=311330810), [XDG Autostart](https://zbrown.pages.freedesktop.org/xdg-specs/autostart-spec/latest/), [KDE Autostart](https://docs.kde.org/stable_kf6/en/plasma-workspace/kcontrol/autostart/index.html)

# Блок 3. Самообновление

## 3а. GitHub Release → `.deb` → polkit/APT

### Правильная схема

1. Непривилегированное приложение получает метаданные релиза и скачивает пакет.
2. Проверяет версию, имя пакета, архитектуру и **криптографическую подпись производителя**.
3. Однократно запускает фиксированный root-helper через `pkexec`.
4. Helper безопасно копирует пакет в root-owned staging, повторно проверяет подпись/хеш и поля `dpkg-deb`, затем выполняет `apt-get install --no-remove /path/package.deb`.
5. GUI завершает старый процесс и предлагает перезапуск. **[рекомендация, H/M]** [pkexec](https://polkit.pages.freedesktop.org/polkit/pkexec.1.html), [apt-get](https://manpages.debian.org/bookworm/apt/apt-get.8.en.html)

`apt install ./file.deb` устанавливает зависимости через APT; `dpkg -i` сам зависимости не разрешает. Для программного helper предпочтительнее стабильный CLI `apt-get`, а не ориентированный на пользователя `apt`. **[H]** [apt-get](https://manpages.debian.org/bookworm/apt/apt-get.8.en.html), [dpkg](https://manpages.debian.org/bookworm/dpkg/dpkg.1.en.html), [apt](https://manpages.debian.org/bookworm/apt/apt.8.en.html)

Не привязывайте собственное действие polkit непосредственно к `/usr/bin/apt`: тогда авторизованный пользователь сможет подставить другие аргументы и установить произвольный пакет. `pkexec` сам аргументы не валидирует. **[H]** [pkexec security notes](https://polkit.pages.freedesktop.org/polkit/pkexec.1.html)

### Политика polkit

Политику устанавливает исходный `.deb` в `/usr/share/polkit-1/actions/io.github.owner.astra_voice.policy`; приложение не должно поставлять JavaScript-rules в `/etc/polkit-1/rules.d`. **[H]** [polkit manual](https://polkit.pages.freedesktop.org/polkit/polkit.8.html)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policyconfig PUBLIC
 "-//freedesktop//DTD PolicyKit Policy Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/PolicyKit/1/policyconfig.dtd">
<policyconfig>
  <vendor>Astra Voice</vendor>
  <action id="io.github.owner.astra_voice.update">
    <description>Обновление Astra Voice</description>
    <message>Для установки обновления требуется авторизация</message>
    <defaults>
      <allow_any>no</allow_any>
      <allow_inactive>no</allow_inactive>
      <allow_active>auth_admin</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">
      /usr/libexec/astra-voice-update-helper
    </annotate>
  </action>
</policyconfig>
```

- `auth_admin` требует одну административную аутентификацию на один вызов helper. Если вся установка выполняется этим вызовом, пользователь видит одно окно пароля. **[H]** [polkit authorization values](https://polkit.pages.freedesktop.org/polkit/polkit.8.html)
- `auth_admin_keep` кеширует успешную авторизацию примерно на несколько минут, но увеличивает окно риска и небезопасен, если решение зависит от изменяемых аргументов. Здесь он не нужен. **[H]** [polkit `_keep`](https://polkit.pages.freedesktop.org/polkit/polkit.8.html)
- Helper должен быть root-owned, недоступным для записи пользователю, лучше небольшим компилируемым ELF; принимать только строго ограниченную команду обновления и проверять symlink/TOCTOU, package name, architecture и допустимость повышения версии. **[рекомендация, M]**
- Для первой установки такой helper ещё отсутствует: первоначальный `.deb` устанавливается обычным системным способом; собственная политика начинает работать со следующих обновлений. **[логический вывод, H]**

### Polkit-агент во Fly/KDE

В штатной графической установке Astra ожидается файл `/etc/xdg/autostart/polkit-kde-authentication-agent-1.desktop` и процесс `polkit-kde-authentication-agent-1`. Astra публикует отдельную инструкцию восстановления именно этого агента при отсутствии окна пароля. **[H]** [Astra KB о polkit-agent](https://wiki.astralinux.ru/kb/pri-autentifikatsii-poyavlyaetsya-predupreyodenie-vmesto-okna-vvoda-parolya-353638228.html), [KDE Polkit Agent](https://github.com/KDE/polkit-kde-agent-1)

Проверять на целевой машине:

```bash
pgrep -af 'polkit.*authentication'
test -r /etc/xdg/autostart/polkit-kde-authentication-agent-1.desktop
```

`pkexec` обращается к агенту зарегистрированной пользовательской сессии; при отсутствии агента возможна текстовая аутентификация, но GUI без терминала на неё рассчитывать не должен. **[H]** [pkexec](https://polkit.pages.freedesktop.org/polkit/pkexec.1.html)

### `DISPLAY`, `XAUTHORITY`

`pkexec` создаёт минимальное безопасное окружение и штатно не переносит `DISPLAY`/`XAUTHORITY`; опция `allow_gui` существует, но прямо не рекомендуется. **[H]** [pkexec environment](https://polkit.pages.freedesktop.org/polkit/pkexec.1.html)

Следствие: root-helper не должен показывать Qt/KDE-окна, подключаться к пользовательскому X-серверу или запускать GUI APT. Всё UI остаётся в непривилегированном процессе, а helper выдаёт машинно-читаемый результат через stdout/pipe. **[рекомендация, H]**

GitHub-пакет не получает доверие автоматически: APT проверяет подпись цепочки `Release/InRelease → Packages → файл` только для репозитория; отдельно загруженный `.deb` необходимо аутентифицировать самостоятельно. **[H]** [apt-secure](https://manpages.debian.org/bookworm/apt/apt-secure.8.en.html)

## 3б. Установка в `~/.local`

- **AppImage:** один переносимый образ, возможна дельта/замена через AppImageUpdate; нужны подходящие kernel/FUSE-возможности. **[H/M]** [запуск AppImage](https://docs.appimage.org/user-guide/run-appimages.html), [AppImage updates](https://docs.appimage.org/packaging-guide/optional/updates.html)
- **Самораспаковывающийся архив:** проще сделать A/B-каталоги `versions/<version>` и атомарно переключать `current`; легко откатить, но весь исполняемый код остаётся изменяемым пользователем. **[архитектурная рекомендация, M]**
- **venv:** малый собственный код, но зависимость от системного Python ABI и множество файлов; обновление pip «на месте» неатомарно, поэтому также нужны versioned venv. **[M]**
- Ни AppImage-подпись, ни GPG-подпись архива, ни подпись wheel не являются подписью `digsig_verif`. **[H]** [Astra КСЗ](https://wiki.astralinux.ru/download/attachments/371790129/3.8_nocert_Ruk_KSZ_1.pdf?api=v2)
- Размещение в home не освобождает объект от проверки ЗПС, мандатного контроля, запрета исполнения или локальной политики меток. Официального документа именно про сочетание AppImage/venv с ЗПС Astra я **не нашёл**; это следует из того, что контроль применяется при исполнении/открытии файла, а не только в `/usr`. **[M]** [Astra КСЗ](https://wiki.astralinux.ru/download/attachments/371790129/3.8_nocert_Ruk_KSZ_1.pdf?api=v2)

Итог: `~/.local` хорош как необязательный community-канал для машин без ЗПС; заявлять его совместимым с защищённой корпоративной конфигурацией нельзя. **[рекомендация, H/M]**

## 3в. Собственный APT-репозиторий

Рекомендуемый deb822-файл:

```text
Types: deb
URIs: https://updates.example.ru/astra-voice
Suites: alse18
Components: main
Architectures: amd64
Signed-By: /usr/share/keyrings/astra-voice-archive.gpg
```

Формат `.sources` поддерживается APT с версии 1.1; `Signed-By` ограничивает источник конкретным keyring. **[H]** [sources.list(5)](https://manpages.debian.org/bookworm/apt/sources.list.5.en.html)

Репозиторий должен публиковать подписанный `InRelease` и корректную цепочку хешей. Закрытый ключ хранится только в CI/repository signer, открытый ключ устанавливается первоначальным `.deb` или администратором. **[H/M]** [apt-secure](https://manpages.debian.org/bookworm/apt/apt-secure.8.en.html)

Кнопка GUI может одним вызовом helper выполнить:

```text
apt-get update
apt-get install --only-upgrade --no-remove astra-voice
```

Но в корпоративной среде лучше оставить регулярный `apt update` и unattended-upgrades системному администратору, а GUI использовать только для проверки `apt-cache policy` и явного обновления. APT имеет собственные periodic-механизмы; приложение не должно самовольно менять общесистемную политику unattended-upgrades. **[H/M]** [apt.conf(5)](https://manpages.debian.org/bookworm/apt/apt.conf.5.en.html), [Astra: структура репозиториев](https://docs.astralinux.ru/latest/guide/compound/repo/)

Критично: GPG-подпись `InRelease` и подпись ЗПС — независимые уровни. Пакет из доверенного APT-репозитория всё равно не запустит неподписанный ELF при `DIGSIG_ELF_MODE=1`. **[H]** [APT](https://manpages.debian.org/bookworm/apt/apt-secure.8.en.html), [Astra КСЗ](https://wiki.astralinux.ru/download/attachments/371790129/3.8_nocert_Ruk_KSZ_1.pdf?api=v2)

## 3г. Flatpak и Snap

- Flatpak на Astra — не теоретическая возможность: у Astra есть официальная документация по `flatpak-builder`, пользовательской установке, GPG-подписанным OSTree-репозиториям и собственный `astraflatpaktools`. **[H]** [Flatpak-builder в Astra](https://docs.astralinux.ru/latest/astraflatpak/flatpak-builder/), [AFT](https://docs.astralinux.ru/latest/astraflatpak/aft/)
- AFT прямо умеет запускать `bsign`/`bsign-integrator` для ELF и отдельно подписывать Flatpak-репозиторий GPG. **[H]** [AFT manifest: `build.bsign`](https://docs.astralinux.ru/latest/astraflatpak/aft/aft-app-manifest/)
- Для голосовой утилиты Flatpak усложнит микрофон, X11, глобальные горячие клавиши и эмуляцию ввода; часть разрешений придётся выдавать явно, а наличие нужных portal-версий на конкретной 1.8.5 надо тестировать. **[M]** [пример Astra permissions: X11/PulseAudio](https://docs.astralinux.ru/latest/astraflatpak/aft/aft-app-manifest/)
- Официальная статья Astra про Snap перечисляет SE 1.8 и установку `snapd`, но помечена **AS IS**, а её wiki-endpoint нестабилен; наличие пакета именно в подключённых репозиториях 1.8.5 удалённо подтвердить не удалось. **[L]** [навигатор официальных пакетов Astra](https://packages.astralinux.ru/search/binary/)
- Практическая проверка образа: `apt-cache policy flatpak flatpak-builder astraflatpaktools snapd`. **[рекомендация, H]**

Для этого проекта Flatpak можно рассматривать позднее; Snap не рекомендую как основной канал Astra. **[вывод, M]**

## 3д. ЗПС / `digsig`

### Что контролируется

- Ядровой модуль `digsig_verif` проверяет подписи при запуске ELF и загрузке разделяемых библиотек. В enforce-режиме отсутствие, недействительность или отзыв подписи запрещают операцию. **[H]** [Astra КСЗ, §13.1](https://wiki.astralinux.ru/download/attachments/371790129/3.8_nocert_Ruk_KSZ_1.pdf?api=v2), [рекомендации, согласованные ФСТЭК](https://wiki.astralinux.ru/download/attachments/53643494/%D0%9C%D0%B5%D1%82%D0%BE%D0%B4%D0%B8%D1%87%D0%B5%D1%81%D0%BA%D0%B8%D0%B5_%D1%80%D0%B5%D0%BA%D0%BE%D0%BC%D0%B5%D0%BD%D0%B4%D0%B0%D1%86%D0%B8%D0%B8_%D0%BF%D0%BE_%D0%BD%D0%B0%D1%81%D1%82%D1%80%D0%BE%D0%B9%D0%BA%D0%B5_AstraLinux_1.8_.pdf?api=v2&modificationDate=1722970838587&version=1)
- `DIGSIG_ELF_MODE=0/1/2` означает выключено/enforce/диагностический режим соответственно. Настройка находится в `/etc/digsig/digsig_initramfs.conf`; после изменения требуется обновить initramfs. **[H]** [Astra КСЗ](https://wiki.astralinux.ru/download/attachments/371790129/3.8_nocert_Ruk_KSZ_1.pdf?api=v2)
- Поддерживаются встроенные ELF-подписи, подписи в extended attributes и отделённые подписи; выбор задаёт `DIGSIG_ELF_VERIFICATION_MODE`. Отделённые подписи размещаются под `/etc/digsig/external_sig`. **[H]** [Astra КСЗ](https://wiki.astralinux.ru/download/attachments/371790129/3.8_nocert_Ruk_KSZ_1.pdf?api=v2)
- `DIGSIG_XATTR_MODE` отдельно контролирует указанные в `/etc/digsig/xattr_control` неисполняемые файлы при открытии. Поэтому скрипты/модели могут попасть под контроль даже при отсутствии ELF. **[H]** [Astra КСЗ](https://wiki.astralinux.ru/download/attachments/371790129/3.8_nocert_Ruk_KSZ_1.pdf?api=v2)

Официальные имена, найденные в документации: `astra-digsig-control`, `digsig_verif`, `bsign` и `bsign-integrator`. Официального SE 1.8 документа, называющего рабочий процесс `astra-digsig-fs` или `digsig-elf`, я не нашёл. **[H/L]** [Astra: bsign-integrator](https://wiki.astralinux.ru/spaces/flyingpdf/pdfpageexport.action?pageId=383203855)

### Ключи и возможность запуска стороннего ПО

- Доверенные ключи ELF хранятся под `/etc/digsig/keys`; после их изменения выполняется `update-initramfs -u -k all`. В поставке также присутствуют первичные/партнёрские ключи, включая партнёрскую иерархию Astra/РусБИТех. **[H]** [Astra КСЗ](https://wiki.astralinux.ru/download/attachments/371790129/3.8_nocert_Ruk_KSZ_1.pdf?api=v2)
- Для стандартного контроля ELF открытый ключ подписанта должен восходить к доверенной иерархии Astra/партнёра. Самоподписанный ключ, добавленный вашим обновлятором без решения администратора, доверенным не становится. **[H]** [Astra КСЗ, §13.1.3–13.1.4](https://wiki.astralinux.ru/download/attachments/371790129/3.8_nocert_Ruk_KSZ_1.pdf?api=v2)
- Дополнительные локальные ключи в `/etc/digsig/xattr_keys` предназначены для XATTR-контроля файлов; ответственность за их доверие несёт администратор. Это не следует трактовать как автоматический обход стандартной проверки ELF. **[H/M]** [Astra КСЗ](https://wiki.astralinux.ru/download/attachments/371790129/3.8_nocert_Ruk_KSZ_1.pdf?api=v2)

**Ответ на ключевой вопрос:** неподписанный сторонний ELF при `DIGSIG_ELF_MODE=1` не запускается. Разработчик должен либо получить совместимую подпись через партнёрскую/Ready for Astra цепочку, либо предоставить заказчику воспроизводимые артефакты, чтобы его уполномоченный интегратор подписал их уже доверенным ключом и ввёл этот ключ по утверждённой процедуре. **[H/M]**

### Python против Rust/Go

- Системный подписанный Python может исполнять обычные `.py`: `DIGSIG_ELF_MODE` проверяет ELF-интерпретатор и ELF-библиотеки, а не содержимое чистого Python-кода. Но `.py` может быть запрещён отдельным XATTR-контролем. **[H/M]** [Astra КСЗ](https://wiki.astralinux.ru/download/attachments/371790129/3.8_nocert_Ruk_KSZ_1.pdf?api=v2)
- Python C-extension в Linux является разделяемой библиотекой `.so`; следовательно, нативные `.so` из wheel должны иметь допустимую подпись. Это касается wheel-вариантов NumPy, ONNX Runtime, PyQt и других пакетов, содержащих native extensions. **[H/M]** [Python C-extension docs](https://docs.python.org/3/c-api/extension-modules.html), [Astra КСЗ](https://wiki.astralinux.ru/download/attachments/371790129/3.8_nocert_Ruk_KSZ_1.pdf?api=v2)
- Venv не помогает: он меняет расположение файлов, но не тип загружаемой `.so`. **[логический вывод, H]**
- У действительно единого статического Rust/Go ELF поверхность подписания минимальна: подписывается один файл. При динамической линковке или поставке дополнительных `.so` подписывать нужно и их. **[H/M]**
- Модели ONNX/Vosk и конфигурации не ELF; они блокируются только если администратор включил XATTR-контроль для соответствующих путей. **[H/M]**

Самообновление при ЗПС возможно только тогда, когда **каждый новый ELF/`.so` уже подписан доверенным ключом до доставки**. Установить неподписанное обновление можно, но его новый процесс или native-модуль не запустится. **[H]** [Astra КСЗ](https://wiki.astralinux.ru/download/attachments/371790129/3.8_nocert_Ruk_KSZ_1.pdf?api=v2)

## 3е. Индикация обновлений

- Для stable использовать `GET /repos/{owner}/{repo}/releases/latest`; endpoint возвращает последний недрафтовый и непререлизный Release и доступен без токена для публичного репозитория. **[H]** [GitHub Releases API](https://docs.github.com/en/rest/releases/releases#get-the-latest-release)
- Анонимный REST API ограничен **60 запросами/час на IP**; токен даёт больший лимит, но секрет нельзя встраивать в desktop-клиент. **[H]** [GitHub rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)
- Проверять автоматически не чаще раза в сутки, кешировать `ETag`, использовать `If-None-Match`, соблюдать `Retry-After`/rate-limit headers и не устраивать циклические повторы. **[рекомендация, H]** [GitHub API best practices](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api)
- `tag_name` нормализовать только в строго определённом виде, например удалить один префикс `v`, затем сравнивать библиотекой SemVer; prerelease ниже соответствующего stable, build metadata на порядок не влияет. **[H]** [SemVer 2.0](https://semver.org/)
- Отсутствие сети, timeout, TLS/proxy error — не ошибка работы приложения: показывать «не удалось проверить» и дату последней успешной проверки, не модальный диалог. **[рекомендация, M]**
- Поддержать системный proxy и `https_proxy`/`NO_PROXY`, не отключать TLS-проверку для корпоративного MITM, а использовать системное доверенное CA-хранилище. **[H/M]** [curl: proxy environment](https://everything.curl.dev/usingcurl/proxies/env.html)
- GitHub заявляет о стремлении сохранять публичные open-source-сервисы доступными в санкционных регионах, но это не гарантия сетевой доступности из РФ. Официального подтверждения стабильной общероссийской блокировки API на дату проверки я не нашёл; гарантировать доступ у всех провайдеров нельзя. **[M]** [GitHub Trade Controls](https://docs.github.com/en/site-policy/other-site-policies/github-and-trade-controls), [GitHub Status](https://www.githubstatus.com/)
- Предусмотрите конфигурируемый base URL и независимое HTTPS-зеркало манифеста/пакетов или собственный APT-репозиторий в доступной юрисдикции. Все зеркала должны проверяться одним закреплённым ключом производителя. **[рекомендация, H/M]**

# Блок 4. Автозапуск

## 4а. XDG Autostart

Fly документирует системный `/etc/xdg/autostart` и пользовательский `$HOME/.config/autostart`; KDE Plasma также использует пользовательский каталог autostart. **[H/M для точной связки 1.8.5]** [Astra Fly](https://wiki.astralinux.ru/spaces/flyingpdf/pdfpageexport.action?pageId=311330810), [KDE](https://docs.kde.org/stable_kf6/en/plasma-workspace/kcontrol/autostart/index.html)

```ini
[Desktop Entry]
Type=Application
Name=Astra Voice
Exec=/usr/bin/astra-voice --autostart
TryExec=/usr/bin/astra-voice
Terminal=false
```

- Пользовательский файл с тем же basename перекрывает системный; `Hidden=true` отключает такую запись. **[H]** [XDG Autostart](https://zbrown.pages.freedesktop.org/xdg-specs/autostart-spec/latest/)
- `OnlyShowIn` разрешает запуск только перечисленным desktop ID, `NotShowIn` исключает их; одновременно задавать оба нельзя. Fly документирует значения `fly`, `fly-mobile`, `fly-tablet`, `fly-weston`, `fly-mini`, `failsafe`. **[H/M]** [Astra Fly](https://wiki.astralinux.ru/spaces/flyingpdf/pdfpageexport.action?pageId=311330810), [XDG](https://zbrown.pages.freedesktop.org/xdg-specs/autostart-spec/latest/)
- Для общей сборки Fly/KDE лучше вообще не задавать `OnlyShowIn`: точное значение `$XDG_CURRENT_DESKTOP` зависит от выбранной сессии. **[рекомендация, M]**
- `X-GNOME-Autostart-enabled` не входит в стандарт XDG и не должен использоваться для управления запуском во Fly/KDE. Стандартный выключатель — `Hidden=true` либо удаление собственного пользовательского файла. **[H/M]** [XDG Autostart](https://zbrown.pages.freedesktop.org/xdg-specs/autostart-spec/latest/)
- Если приложение само создало пользовательский файл, включение — атомарная запись/rename с правами пользователя; выключение — удаление. Если пользовательский файл маскирует системный, выключение делается файлом того же имени с `Hidden=true`. **[H/M]**
- `Exec` не является строкой shell; аргументы нужно экранировать по Desktop Entry Specification, а не вставлять через `sh -c`. **[H]** [Desktop Entry Specification](https://specifications.freedesktop.org/desktop-entry/latest-single/)

## 4б. `systemd --user`

Astra 1.8 использует systemd 252 и документирует пользовательские units в `/usr/lib/systemd/user`, `/etc/systemd/user` и управление linger. **[H]** [Astra: systemd](https://wiki.astralinux.ru/spaces/flyingpdf/pdfpageexport.action?pageId=383204309)

```ini
[Unit]
Description=Astra Voice
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/bin/astra-voice --autostart
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical-session.target
```

- `graphical-session.target` — пассивная цель: её должен активировать desktop/session manager. KDE Plasma 5.27 имеет systemd-интеграцию, но официального подтверждения, что именно Fly-сессия Astra 1.8.5 всегда активирует этот target, я не нашёл. **[H/M]** [systemd.special](https://man7.org/linux/man-pages/man7/systemd.special.7.html), [Plasma 5.27 service source в Debian](https://sources.debian.org/src/plasma-workspace/4%3A5.27.5-2%2Bdeb12u2/ksmserver/plasma-ksmserver.service.in/)
- `loginctl enable-linger` для приложения «при входе в GUI» не нужен: linger запускает user manager уже при загрузке и сохраняет после logout, то есть может стартовать сервис без дисплея. **[H]** [loginctl](https://www.freedesktop.org/software/systemd/man/252/loginctl.html)
- Нельзя задавать `DISPLAY=:0` или фиксированный `XAUTHORITY`: это ломает multi-seat и параллельные сессии. Окружение следует импортировать из Xsession через `dbus-update-activation-environment --systemd DISPLAY XAUTHORITY XDG_CURRENT_DESKTOP`. **[H/M]** [D-Bus environment update](https://dbus.freedesktop.org/doc/dbus-update-activation-environment.1.html), [Astra: Fly Xsession](https://wiki.astralinux.ru/spaces/flyingpdf/pdfpageexport.action?pageId=217200727)
- Если импорт выполняет само приложение, для его первого запуска это уже слишком поздно; импорт должен делать desktop/Xsession. **[логический вывод, H]**

Проверка конкретного образа:

```bash
systemctl --user is-system-running
systemctl --user is-active graphical-session.target
systemctl --user show-environment |
  grep -E '^(DISPLAY|XAUTHORITY|XDG_CURRENT_DESKTOP)='
grep -R pam_systemd.so /etc/pam.d/fly-dm /etc/pam.d/common-session
```

## 4в. Что надёжнее для трея

Для GUI/X11-трея надёжнее XDG Autostart: его запускает уже созданная графическая сессия с правильными `DISPLAY`, D-Bus и авторизацией X11. `systemd --user` выигрывает рестартами и журналом, но зависит от интеграции desktop target/environment, которая для Fly 1.8.5 официально не подтверждена. **[вывод, M]**

Qt `QSystemTrayIcon` умеет добавить уже созданный видимый значок, когда системный tray появляется позднее; поэтому лучше реагировать на появление tray, а не использовать большой фиксированный `sleep`. **[H/M для Qt 5.15]** [QSystemTrayIcon](https://doc.qt.io/qt-6/qsystemtrayicon.html), [freedesktop System Tray](https://specifications.freedesktop.org/systemtray/latest-single/)

## 4г. Ловушки

- Не включать одновременно XDG Autostart и systemd unit. Всё равно реализовать single-instance через well-known имя session D-Bus; второй процесс вызывает `Activate` у первого и завершается. **[H/M]** [Qt D-Bus registration](https://doc.qt.io/qt-6/qdbusconnection.html)
- Резервный lock размещать в `$XDG_RUNTIME_DIR`, а не в home: runtime-dir принадлежит пользователю, локален и существует в течение login-сессии. Для stale-lock использовать `QLockFile`. **[H]** [XDG Base Directory](https://specifications.freedesktop.org/basedir/0.8/), [QLockFile](https://doc.qt.io/qt-6/qlockfile.html)
- KDE session restore, XDG и ручной запуск могут пересечься; D-Bus singleton должен делать это безвредным. **[M]**
- Файл `~/.config/autostart/*.desktop` следует создавать от имени вошедшего пользователя. Root-helper не должен менять его метки или владельца. **[рекомендация, M]**
- Специального официального документа Astra о мандатных метках именно каталога `~/.config/autostart` я не нашёл. Практически доступ зависит от метки текущей сессии и родительского каталога; диагностировать следует штатными средствами администратора (`getfmac`), не пытаясь обходить политику из приложения. **[M/L]** [руководство КСЗ Astra 1.8](https://wiki.astralinux.ru/download/attachments/371790129/3.8_nocert_Ruk_KSZ_1.pdf?api=v2)

---

# Блок 5. GUI-стек и системные интеграции

Срез источников: **7 сентября 2026 года**. Версии Astra/Qt/GTK/Python считаю подтверждёнными вашим стендом.

Обозначения: **high** — прямо следует из документации/кода; **med** — вывод из первичного источника плюс архитектурная интерпретация; **low** — прогноз без замера на целевой машине.

Для ЗПС важно считать именно ELF: Astra проверяет подписи **исполняемых ELF и разделяемых библиотек**, а неподписанные объекты в штатном режиме не загружаются ([Astra Linux, ЗПС](https://docs.astralinux.ru/latest/szi/szi/cse/), **high**).

В таблицах:

- **B** — собственные ELF/`.so`, поставляемые внутри вашего `.deb`; именно их придётся включать в процесс подписи.
- **S** — системные `.so` из репозитория Astra, загружаемые динамически; обычно они уже входят в доверенную поставку ОС.
- Числа B/S, размеры и cold start — **инженерный прогноз**, не результат измерения.

## 5.1. GUI-стек

| Стек | Вид и стилизация | `.deb`, ABI, лицензия | ЗПС / «один ELF» | Прогноз размера и старта |
|---|---|---|---|---|
| **PyQt5 5.15.9 из apt + QSS** | Widgets выглядят традиционно; современность придётся сделать собственной темой, spacing, SVG-иконками и анимациями. QSS каскадно стилизует всё приложение и способен заменить часть `QStyle` ([Qt Style Sheets](https://doc.qt.io/qt-6/stylesheet.html), **high**). | Простой обычный `.deb`: Python-код плюс зависимости на системные `python3-pyqt5`. PyQt лицензируется GPLv3 либо коммерчески, не LGPL ([Riverbank](https://www.riverbankcomputing.com/software/pyqt/), **high**). | GUI: **B≈0**, S≈20–40. Не один ELF: системный Python, PyQt-модули и Qt `.so`; зато собственных неподписанных GUI-библиотек нет (**оценка, med**). | Код/ресурсы 1–5 МБ без модели; 0,15–0,6 с (**оценка, low**). Самый низкий порог входа. |
| **PyQt6 из pip** | Widgets почти как Qt5; Qt Quick даёт Material/Universal/Basic, причём Basic позиционируется как лёгкая база для кастомизации ([Qt Quick styles](https://doc.qt.io/qt-6.5/qtquickcontrols-styles.html), **high**). | Текущие x86-64 wheels PyQt6/PyQt6-Qt6 имеют `manylinux_2_34`; glibc 2.36 удовлетворяет правилу «glibc X.Y или новее» ([PyQt6-Qt6](https://pypi.org/project/PyQt6-Qt6/), [PEP 600](https://peps.python.org/pep-0600/), **high**). Лицензия PyQt — GPL/commercial. | **B≈50–100** ELF/`.so` с Qt-плагинами, S≈10–25; далеко не один ELF (**оценка, low**). Для ЗПС существенно хуже apt-варианта. | Сжатые wheels порядка 8 МБ bindings + 86 МБ Qt; установленный объём больше ([PyQt6](https://pypi.org/project/PyQt6/), [Qt wheel](https://pypi.org/project/PyQt6-Qt6/), **high**). Старт 0,2–0,8 с (**low**). |
| **PySide6 из pip** | Можно использовать Widgets или QML/Quick; Qt рекомендует Widgets как зрелый традиционный GUI, Quick — для fluid/declarative UI ([Qt for Python](https://doc.qt.io/qtforpython-6/gettingstarted.html), **high**). | PySide6 — LGPLv3/GPLv3/comмерческая лицензия ([Qt for Python](https://doc.qt.io/qtforpython-6/), **high**). Текущие wheels — `manylinux_2_34`, совместимы с glibc 2.36 ([Essentials](https://pypi.org/project/PySide6-Essentials/), [PEP 600](https://peps.python.org/pep-0600/), **high**). | Только Essentials: **B≈70–130**; полный meta-package с Addons может превысить 150 объектов (**оценка, low**). Qt и bindings находятся внутри wheel ([структура пакетов](https://doc.qt.io/qtforpython-6/package_details.html), **high**). | Essentials ≈80 МБ compressed; Addons ≈175 МБ ([PyPI Essentials](https://pypi.org/project/PySide6-Essentials/), [Addons](https://pypi.org/project/PySide6-Addons/), **high**). Старт 0,25–0,9 с (**low**). |
| **Qt6 C++ 6.4.2 + QML/Quick** | Наилучший современный нативный вариант: QML, transitions, Material/Basic, HiDPI. Qt Quick Controls входят в отдельный QML-модуль; Debian Bookworm действительно имеет пакет версии 6.4.2 ([qml6-module-qtquick-controls](https://packages.debian.org/bookworm/qml6-module-qtquick-controls), **high**). Наличие именно в Astra надо проверить через `apt-cache policy`; публичного подтверждения не нашёл (**low**). | CMake/CPack или `dpkg-buildpackage`; системный Qt не надо вкладывать. QML deployment поддерживается с Qt 6.3 ([Qt QML deployment](https://doc.qt.io/qt-6/cmake-deployment.html), **high**). | **B=1 ELF** для GUI, S≈25–50; с статически включённым sherpa — близко к одному собственному ELF, но glibc/Qt/X11 остаются динамическими (**оценка, med**). | `.deb` приложения 2–15 МБ без ASR и моделей; 0,05–0,3 с (**оценка, low**). Порог: C++/QML/CMake, выше Python. |
| **Tauri 2 + WebKitGTK** | HTML/CSS дают самый простой путь к виду Handy. Используется системный WebKitGTK, а не вложенный Chromium ([Tauri Linux prerequisites](https://v2.tauri.app/start/prerequisites/), **high**). | Tauri 2 требует ABI `webkit2gtk-4.1`/libsoup3; `.deb` объявляет зависимость `libwebkit2gtk-4.1-0` ([Tauri Debian](https://v2.tauri.app/distribute/debian/), **high**). WebKitGTK 2.50 новее используемой Tauri нижней границы; работа на 2.50.x подтверждается в текущих отчётах wry, хотя встречаются драйверные проблемы ([wry #1727](https://github.com/tauri-apps/wry/issues/1727), **med**). Проверить надо именно `pkg-config webkit2gtk-4.1`, одной строки «2.50» недостаточно. | **B≈1** основной ELF плюс speech `.so`; S≈60–120. Поставляемый GUI почти «один ELF», но WebKit запускает несколько системных процессов (**оценка, med**). | 3–20 МБ без модели; 0,2–0,9 с (**оценка, low**). Риск — нестабильность поведения при обновлениях WebKitGTK и возвращение Rust+frontend toolchain, от которого проект хотел уйти. |
| **egui / iced / Slint** | egui — полностью кастомизируемый immediate-mode UI, но по умолчанию выглядит как инструмент разработчика ([egui](https://github.com/emilk/egui), **high**). iced — Elm-подобная декларативная архитектура и `wgpu`/software renderer ([iced](https://github.com/iced-rs/iced), **high**). Slint визуально ближе всего к QML, имеет declarative UI и X11 backend ([Slint](https://github.com/slint-ui/slint), **high**). | Cargo легко оборачивается в `.deb`. Большая часть Rust-зависимостей линкуется в приложение, но glibc, X11, GL/ALSA остаются системными (**med**). Slint: GPLv3, royalty-free desktop license с условиями либо commercial ([лицензия Slint](https://github.com/slint-ui/slint/blob/master/LICENSE.md), **high**). | **B=1 ELF** при статическом sherpa; S≈8–25. Лучший вариант по количеству подписываемых объектов, но не буквально статический Linux ELF (**оценка, med**). | 8–40 МБ; 0,03–0,3 с (**оценка, low**). Риски: менее зрелые tray/a11y/IME-интеграции; у egui нет нативного KDE-вида. |
| **GTK4 + libadwaita** | Современный GNOME-интерфейс почти из коробки: preferences, toast, adaptive widgets, CSS ([libadwaita PreferencesWindow](https://gnome.pages.gitlab.gnome.org/libadwaita/doc/main/class.PreferencesWindow.html), [styles](https://gnome.pages.gitlab.gnome.org/libadwaita/doc/1.5/styles-and-appearance.html), **high**). В Plasma будет выглядеть «как GNOME», а не как KDE. | Debian Bookworm с GTK 4.8.3 поставляет libadwaita 1.2.2 ([GTK4](https://packages.debian.org/bookworm/source/gtk4), [libadwaita](https://packages.debian.org/bookworm/libadwaita-1-0), **high для Debian**). Точную версию в Astra 1.8.5 публично не нашёл: проверить `apt-cache policy libadwaita-1-0 gir1.2-adw-1`. 1.2 достаточно для обычного settings UI (**med**). | На C/Rust: **B=1**, S≈30–60; на Python с PyGObject B≈0 собственного GUI, но не один ELF (**оценка, med**). | 1–10 МБ приложения; 0,05–0,35 с (**low**). Интеграция с KDE слабее Qt. |
| **Electron** | CSS даёт полную свободу, но приложение везёт Chromium и Node.js ([Electron architecture](https://www.electronjs.org/docs/latest/), **high**). | `.deb` собирается штатно, но распространяется полный Electron runtime ([distribution](https://www.electronjs.org/docs/latest/tutorial/application-distribution), **high**). | **B≈8–20** ELF/`.so`, много процессов; точно не один ELF (**оценка, low**). | 90–180 МБ без модели, 0,6–2,5 с (**оценка, low**). Для небольшого always-running PTT-клиента неоправдан. |

**Вывод 5.1:** pip-Qt6 технически совместим с glibc 2.36, но организационно неудобен для ЗПС. Если важнее современный интерфейс и минимальное число собственных ELF, выигрывает системный **Qt6 C++/QML**; если важнее скорость разработки — **PyQt5 из apt**.

## 5.2. Глобальный PTT на X11

- **XGrabKey** — лучший базовый механизм для назначенного сочетания: создаёт passive grab; при конфликте с уже захваченным сочетанием сервер возвращает `BadAccess` ([XGrabKey](https://xorg.freedesktop.org/archive/X11R6.7.0/doc/XGrabKey.3.html), **high**). После `KeyPress` активный grab позволяет получить соответствующий `KeyRelease`.
- **XRecord** слушает глобальные `DeviceKeyPress/DeviceKeyRelease`, не захватывая сочетание ([X Record protocol](https://xorg.freedesktop.org/archive/X11R7.7/doc/recordproto/record.html), **high**). Подходит для modifier-only, но клавиша продолжает действовать в целевом приложении и выглядит для аудита как глобальный keylogger.
- **XI2 RawKeyPress/RawKeyRelease** даёт физические keycode и события независимо от окна; raw-события не содержат готового modifier state ([XI2 constants](https://github.com/freedesktop/xorgproto/blob/master/include/X11/extensions/XI2proto.h), **med**). Это хороший резерв для правого Ctrl, но не эксклюзивный хоткей.
- **evdev** сообщает `EV_KEY`: 0=release, 1=press, 2=autorepeat ([Linux input API](https://kernel.org/doc/html/latest/input/input.html), **high**). Нужны права на `/dev/input/event*`, ACL/udev либо группа `input`; такой доступ позволяет читать всю клавиатуру, поэтому корпоративно это худший вариант (**оценка безопасности, high**).
- **pynput/python-xlib** в Linux опираются на X11/XRecord/XTest; исходник pynput обрабатывает press/release ([pynput Xorg backend](https://github.com/moses-palmer/pynput/blob/master/lib/pynput/keyboard/_xorg.py), **high**). Удобно для прототипа, но добавляет Python-слой и меньше контроля над grabs.
- **rdev** на Linux использует XRecord и формирует события Press/Release ([реализация](https://docs.rs/rdev/latest/src/rdev/linux/listen.rs.html), **high**): подходит для наблюдения, не для предотвращения конфликта.
- **global-hotkey** поддерживает Linux/X11 и состояния Pressed/Released ([репозиторий](https://github.com/tauri-apps/global-hotkey), [event API](https://docs.rs/global-hotkey/latest/global_hotkey/struct.GlobalHotKeyEvent.html), **high**). Это лучший готовый Rust-вариант для обычных chords.

Надёжный автомат:

```text
Idle --первый press--> Recording --release--> Recognizing
        повторы игнорировать       timeout/focus-loss → принудительный stop
```

X11 autorepeat может давать последовательности release/press, если не включён detectable repeat ([XKB specification](https://www.x.org/releases/X11R7.6/doc/libX11/specs/XKB/xkblib.pdf), **high**). Поэтому хранить собственный `is_down`, фильтровать повтор и не переключать запись по каждому событию.

Практические правила:

- По умолчанию назначить редкое сочетание вроде `Ctrl+Alt+Space` или `F13`; при `BadAccess` показать явную ошибку и предложить другое. Нельзя молча перехватывать KDE shortcut (**вывод из XGrabKey, high**).
- Для **правого Ctrl** использовать физический keycode через XI2/XKB; high-level библиотеки часто сводят левый/правый Ctrl к одному modifier. В Handy такое различение также запрашивалось отдельно ([Handy discussion #211](https://github.com/cjpais/Handy/discussions/211), **med**).
- Modifier-only через `XGrabKey` технически возможен, но сам Ctrl/CapsLock будет влиять на приложения. Для CapsLock надо учитывать `LockMask`, NumLock и grab-варианты масок.
- **Fn** часто обрабатывается прошивкой и вообще не приходит в X11. Разрешать его только после диагностического теста `xev`/XI2/evdev; универсального решения нет (**опыт модели, med**).

**Handy:** текущий Linux default — `Ctrl+Space`, backend Tauri; предусмотрены Tauri и экспериментальный `handy_keys` ([settings.rs](https://github.com/cjpais/Handy/blob/main/src-tauri/src/settings.rs), [shortcut/mod.rs](https://github.com/cjpais/Handy/blob/main/src-tauri/src/shortcut/mod.rs), **high**). `handy_keys` передаёт состояние down/up и допускает более свободные клавиши, но modifier-only исторически считался проблемным ([Handy issue #6](https://github.com/cjpais/Handy/issues/6), **med**).

**Рекомендация:** `XGrabKey`/`global-hotkey` для штатных сочетаний; отдельный XI2 backend только для physical-key режима. evdev не включать по умолчанию.

## 5.3. Системный трей

- SNI — объект на session DBus, регистрируемый у `StatusNotifierWatcher`; это современный KDE-механизм ([SNI specification](https://specifications.freedesktop.org/status-notifier-item/latest/status-notifier-item.html), **high**).
- Plasma содержит watcher и `xembed-sni-proxy`, то есть поддерживает SNI и преобразует старые XEmbed-иконки ([plasma-workspace](https://github.com/KDE/plasma-workspace), **high**).
- `QSystemTrayIcon` на Linux использует SNI, если окружение его поддерживает, иначе XEmbed; если tray появляется позже, Qt добавляет уже видимую иконку автоматически ([Qt documentation](https://doc.qt.io/qt-6/qsystemtrayicon.html), **high**).
- **Fly-DM — дисплейный менеджер, а не реализация панели**; при сессии Plasma 5.27 решающей является панель Plasma. Для отдельной панели Fly точного публичного подтверждения SNI не нашёл. Документация Astra показывает использование `QSystemTrayIcon` в приложениях Fly, что подтверждает tray как функцию, но не конкретный протокол ([Astra notifications](https://docs.astralinux.ru/latest/interaction_os/notification_center/notifications/), **med**).
- `libayatana-appindicator` реализует indicator/SNI API ([Ayatana](https://github.com/AyatanaIndicators/libayatana-appindicator), **high**); `ksni` — лёгкая Rust-реализация SNI поверх DBus ([ksni](https://github.com/iovxw/ksni), **high**); `pystray` предпочитает AppIndicator и имеет Xorg fallback, но возможности меню backend’ов различаются ([pystray](https://github.com/moses-palmer/pystray), **high**).

Для автозапуска: создавать tray после подключения session DBus, держать `Status=Active`, использовать установленную hicolor-иконку и повторно регистрироваться при смене владельца watcher. В Qt достаточно не уничтожать `QSystemTrayIcon`; в собственной SNI-реализации нужна обработка появления watcher (**архитектурная рекомендация, med**).

## 5.4. Оверлей записи

Для Qt:

```cpp
Qt::Tool | Qt::FramelessWindowHint |
Qt::WindowStaysOnTopHint | Qt::WindowDoesNotAcceptFocus
WA_ShowWithoutActivating = true
WA_TranslucentBackground = true
```

Эти флаги документированы Qt; также существуют `WindowTransparentForInput`, `WA_X11DoNotAcceptFocus` и `X11BypassWindowManagerHint` ([Qt window flags/attributes](https://doc.qt.io/qt-6/qt.html), **high**).

Рекомендуемое поведение:

- Сначала использовать управляемое WM окно с `ABOVE`, `SKIP_TASKBAR`, `SKIP_PAGER`; эти состояния определены EWMH ([EWMH](https://specifications.freedesktop.org/wm/latest-single/), **high**).
- `X11BypassWindowManagerHint` оставить fallback: unmanaged-окно сложнее правильно разместить на нескольких мониторах и оно обходит политику WM (**опыт модели, med**).
- Для полного click-through применить `WindowTransparentForInput` либо пустую input region расширения X Shape ([X Shape](https://xorg.freedesktop.org/archive/X11R7.7/doc/xextproto/shape.html), **high**).
- До показа сохранить `_NET_ACTIVE_WINDOW`; оверлей никогда не активировать. Вставлять в сохраненное окно только если оно ещё существует; иначе использовать текущее активное окно (**рекомендация, med**).

Handy создаёт overlay без рамки, always-on-top, transparent, skip-taskbar и `focusable(false)` ([overlay.rs](https://github.com/cjpais/Handy/blob/main/src-tauri/src/overlay.rs), **high**). Это правильная модель; на X11 не следует рассчитывать на Wayland-specific `gtk-layer-shell`.

## 5.5. Вставка текста

Приоритет для русского Unicode:

1. **Буфер обмена + синтетический paste** — самый надёжный общий путь: Unicode передаётся как UTF-8/MIME и не зависит от текущей раскладки.
2. **`xdotool type`/XTEST** — fallback для полей, запрещающих paste.
3. AT-SPI/IBus — специализированные opt-in backend’ы, не универсальный основной путь.
4. `XSendEvent` — последний резерв.

Подробности:

- `XTestFakeKeyEvent` отправляет keycode press/release, а не Unicode ([XTEST](https://www.x.org/releases/X11R7.5/doc/man/man3/XTestFakeKeyEvent.3.html), **high**).
- `xdotool type` временно сопоставляет отсутствующие keysyms с keycodes, но документация предупреждает о неверных символах на нестандартных раскладках; `--window` переходит на `XSendEvent`, который многие приложения игнорируют ([xdotool manual](https://github.com/jordansissel/xdotool/blob/main/xdotool.pod), [xdo API](https://github.com/jordansissel/xdotool/blob/main/xdo.h), **high**).
- Поэтому кириллица при английской раскладке через посимвольный XTEST потенциально ломает временную keymap, dead keys и shortcuts; clipboard избегает этой проблемы (**вывод, high**).
- X11 clipboard — асинхронная selection ownership; приложение должно обслуживать event loop, пока получатель запросит данные ([ICCCM](https://www.x.org/releases/current/doc/xorg-docs/icccm/icccm.html), [QClipboard](https://doc.qt.io/qt-6/qclipboard.html), **high**).

Правильное восстановление clipboard:

- Сохранить не только текст, а все доступные MIME-форматы.
- Поставить распознанный текст, послать paste, дождаться запроса/изменения ownership и только затем восстановить.
- Фиксированная задержка 100–300 мс остаётся эвристикой; медленные Electron/Java/LibreOffice могут запросить данные позже (**опыт модели, med**).

Ловушки:

- Терминалы часто используют `Ctrl+Shift+V`; нужны профиль приложения, настраиваемый shortcut или `Shift+Insert` (**опыт модели, high**).
- Electron, Swing и LibreOffice обычно принимают clipboard+XTEST, но различаются задержками и обработкой фокуса (**опыт модели, med**).
- Password/secure fields и приложения с собственной политикой clipboard могут отказать.
- IBus `commit_text` предназначен для реализации input-method engine, а не произвольного внешнего инжектора ([IBusEngine](https://ibus.github.io/docs/ibus-1.5/IBusEngine.html), **high**).
- AT-SPI `EditableText.InsertText` работает лишь для виджетов, реализующих accessibility interface ([AT-SPI EditableText](https://ubuntu.com/desktop/docs/en/latest/reference/accessibility/dbus/org.a11y.atspi.EditableText/), **high**).
- `ydotool` работает и под X11, но использует `/dev/uinput` и daemon, обычно требующий привилегий ([ydotool](https://github.com/ReimuNotMoe/ydotool), **high**). Для корпоративной Astra это хуже XTEST.

Handy на Linux выбирает `xdotool`, затем `ydotool`; clipboard-режим сохраняет текст либо изображение, вставляет, ждёт и восстанавливает, но не сохраняет произвольный полный набор MIME ([clipboard.rs](https://github.com/cjpais/Handy/blob/main/src-tauri/src/clipboard.rs), **high**). Есть открытая проблема с raw-keycode ydotool при переназначенной XKB-клавише Ctrl ([Handy #2004](https://github.com/cjpais/Handy/issues/2004), **high**).

## 5.6. Захват звука и VAD

- В Astra 1.8 развивалась поддержка PipeWire, включая микрофоны, но публичного источника, однозначно утверждающего «1.8.5 всегда использует PipeWire как default audio server», я не нашёл ([релиз Astra 1.8.5](https://astra.ru/about/press-center/news/astra-linux-1-8-5-novyy-profil-yadra-performance-kontrol-setevykh-podklyucheniy-i-razvitie-sredstv-v/), **med**).
- PipeWire предоставляет PulseAudio-совместимый сервер `pipewire-pulse` ([PipeWire documentation](https://pipewire.pages.freedesktop.org/pipewire/page_man_pipewire-pulse_conf_5.html), **high**). Поэтому приложение следует писать к Pulse/PortAudio abstraction и во время диагностики показывать результаты `pactl info` и `wpctl status`.
- ALSA напрямую даёт минимальный слой, но хуже делит устройство с другими приложениями; использовать её как fallback, не основной пользовательский backend (**опыт модели, med**).
- `sounddevice` предоставляет PortAudio device enumeration и callback/RawInputStream ([documentation](https://python-sounddevice.readthedocs.io/), **high**); PyAudio тоже оборачивает PortAudio, но API старее и многословнее ([PyAudio](https://people.csail.mit.edu/hubert/pyaudio/docs/), **high**).
- Rust `cpal` на Linux имеет ALSA/JACK backend и перечисляет поддерживаемые конфигурации устройств ([cpal](https://github.com/RustAudio/cpal), **high**); нативного PipeWire backend по умолчанию нет.

Архитектура записи:

- Открывать input stream при запуске приложения и постоянно держать кольцевой буфер **200–300 мс**.
- При hotkey-down копировать pre-roll и продолжать запись; не открывать устройство только после нажатия.
- Захватывать в нативной частоте устройства, часто 48 кГц, затем качественно ресемплировать в 16 кГц mono float32. Запрашивать 16 кГц напрямую только если устройство это гарантированно поддерживает.
- Callback лишь копирует фреймы в ring buffer; ASR, ресемплинг и UI выполняются вне audio thread. PortAudio отдельно предупреждает об ограничениях callback real-time потока ([PortAudio API](https://portaudio.com/docs/v19-doxydocs/portaudio_8h.html), **high**).

**VAD для push-to-talk не обязателен:** границы задаёт пользователь. Он полезен только для удаления тишины, отказа от пустой записи и небольшого trailing trim; нельзя позволять VAD отбрасывать pre-roll (**архитектурная рекомендация, high**).

- **Silero VAD:** MIT, ONNX/JIT, модели порядка 1–2 МБ; sherpa-onnx публикует также int8 около 208 КБ ([Silero](https://github.com/snakers4/silero-vad), [sherpa model list](https://k2-fsa.github.io/sherpa/onnx/vad/silero-vad.html), **high**). Лучший опциональный выбор.
- **WebRTC VAD:** 16-bit mono, 8/16/32/48 кГц, кадры 10/20/30 мс ([py-webrtcvad](https://github.com/wiseman/py-webrtcvad), **high**). Очень лёгкий, но хуже на сложном шуме (**опыт модели, med**).
- **TEN VAD:** 16 кГц, низкая задержка, поставляется как отдельная native-библиотека ([ten-vad](https://github.com/TEN-framework/ten-vad), **high**). Добавляет `.so` и дополнительные лицензионные условия; для ЗПС выгода сомнительна.

## 5.7. sherpa-onnx и GigaAM v3

### Python

На PyPI имеется wheel `sherpa_onnx-1.13.7-cp311-...-manylinux_2_17_x86_64`, около **4,4 МБ** ([PyPI files](https://pypi.org/project/sherpa-onnx/#files), **high**). `manylinux_2_17` гарантирует glibc 2.17+, поэтому glibc 2.36 совместима ([PEP 600](https://peps.python.org/pep-0600/), **high**).

Современная установка разделена на bindings/core/bin; суммарная загрузка CPU runtime заметно больше одного wheel — ориентировочно **35 МБ** ([официальная инструкция](https://k2-fsa.github.io/sherpa/onnx/python/install.html), **med**). Для ЗПС ожидать примерно **B≈2–4 native `.so`**, но точное число надо зафиксировать после скачивания конкретной версии командой `find ... -type f -exec file {} +`; без изучения самих wheels точное число не подтверждено (**low**).

### C/C++

sherpa-onnx предоставляет C и C++ API на Linux ([поддерживаемые API](https://github.com/k2-fsa/sherpa-onnx), **high**). Официальная Linux-сборка поддерживает статические библиотеки; это позволяет включить sherpa и ONNX Runtime в один ELF приложения ([Linux build documentation](https://github.com/k2-fsa/sherpa-onnx/blob/master/docs/source/onnx/install/linux.rst), **high**).

[GitHub Releases](https://github.com/k2-fsa/sherpa-onnx/releases) содержит готовые артефакты для множества языков/платформ, но стабильного обещания одного универсального C-API tarball с ABI для Astra я не нашёл. Для ЗПС надёжнее воспроизводимая сборка из закреплённого commit, чем скачивание случайного release archive (**рекомендация, med**).

### Rust

Сейчас существует официальный safe wrapper `sherpa-onnx` и низкоуровневый `sherpa-onnx-sys` ([Rust API](https://docs.rs/sherpa-onnx/latest/sherpa_onnx/), [sys crate](https://docs.rs/sherpa-onnx-sys/latest/sherpa_onnx_sys/), **high**). По умолчанию Linux-сборка может скачать готовую shared library, поэтому для ЗПС надо явно контролировать static/shared режим и артефакты.

`sherpa-rs` — сторонний wrapper с собственной версионной матрицей и вариантами static/shared linking ([репозиторий](https://github.com/thewh1teagle/sherpa-rs), **high**). Для нового проекта предпочтительнее официальный crate; зрелость Rust API всё равно ниже C/Python из-за меньшей истории и дополнительного FFI-слоя (**оценка, med**).

### GigaAM v3

Официальный GigaAM содержит `v3_rnnt` и `v3_e2e_rnnt`, экспорт в ONNX и собственный greedy RNN-T decoder ([GigaAM](https://github.com/salute-developers/GigaAM), [onnx_utils.py](https://github.com/salute-developers/GigaAM/blob/main/gigaam/onnx_utils.py), **high**).

sherpa-onnx официально документирует **GigaAM v2** как `OfflineRecognizer`, transducer типа `nemo_transducer` ([offline ASR documentation](https://github.com/k2-fsa/sherpa-onnx/blob/master/sherpa-onnx/c-api/docs/offline-asr.dox), **high**). В актуальном официальном каталоге отдельной страницы **GigaAM v3 не нашёл** ([offline transducer models](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/offline-transducer/index.html), **high**).

Рабочие конверсии v3 существуют как community-модели: они используют `OfflineRecognizer`, `model_type="nemo_transducer"` и int8 encoder порядка 300 МБ ([community model card](https://huggingface.co/pantinor/gigaam-v3), [govorun-lite](https://github.com/amidexe/govorun-lite), **med**). Следовательно, архитектура поддерживается, но конкретный набор v3 ONNX/tokens — не официальный sherpa preset; его надо зафиксировать checksum’ами и регрессионным аудионабором.

`v3_e2e_rnnt` обучается на verbatim-тексте с регистром и пунктуацией, тогда как обычная нормализация удаляет пунктуацию и приводит текст к нижнему регистру ([GigaAM training notes](https://github.com/salute-developers/GigaAM/blob/main/train_utils/README.md), **high**). Поэтому для диктовки брать **e2e RNNT**. Отдельной готовой русской punctuation-модели sherpa в официальном каталоге я не нашёл; доступные примеры в основном английские/китайско-английские ([punctuation models](https://github.com/k2-fsa/sherpa/blob/master/docs/source/onnx/punctuation/pretrained_models.rst), **med**).

Напрямую через ONNX Runtime сделать inference реально: официальный `onnx_utils.py` уже показывает feature extraction, состояния и greedy loop. Но собственный production decoder потребует точно повторить preprocessing, tokenizer, state tensors, blank/termination, quantized I/O и обработку нескольких символов на frame; beam search, hotwords и тестирование существенно увеличат объём работ. ONNX Runtime сам предоставляет исполнение графов, а не готовую семантику RNN-T ([ORT Python API](https://onnxruntime.ai/docs/api/python/), **high**). Рационально оставаться на sherpa.

## TOP-2 рекомендация

1. **Qt6 C++ 6.4.2 + QML/Qt Quick Controls + sherpa-onnx C API, статически включённый в приложение.** Лучший production-вариант: современный интерфейс, быстрый старт, системные Qt/X11-библиотеки, обычный `.deb` и всего один собственный ELF для подписи. Hotkey — `XGrabKey` с XI2 fallback; tray — `QSystemTrayIcon`; overlay — Qt flags/EWMH; ввод — clipboard+XTEST. Главные риски: более высокая стоимость C++/QML-разработки, необходимость проверить наличие всех `qml6-module-*` в Astra и воспроизводимо собрать статический sherpa/ORT.

2. **PyQt5 из apt + QSS + Python wheel sherpa-onnx.** Лучший путь к быстрому MVP и поддерживаемой корпоративной поставке: GUI и Qt уже системные, `.deb` прост, tray/overlay/X11 хорошо покрываются Qt. Подписывать, вероятно, придётся лишь 2–4 native-объекта sherpa, но это надо подтвердить на закреплённом wheel. Главные риски: PyQt требует GPL либо коммерческую лицензию, современный вид потребует собственной дизайн-системы QSS, а Python/native wheels менее удобны для строгой ЗПС, чем один C++ ELF.
