# Каталог моделей v1 — таблица-источник истины (P2, факт-чек)

> 2026-09-08 · Researcher · вход: `PRD.md` §7.1–7.2, `research/tech-models-claude.md` (A1), макет `design/mockups/final/02-models-catalog.html`.
> Ничего не скачивалось: только HF API (`?blobs=true`), исходники доков onnx-asr, статья Whisper.
> **Единицы:** МБ = десятичные (10⁶ Б). Почти все «расхождения» ниже — это МиБ, записанные как МБ.

## 0. Протокол — что реально даёт бенчмарк onnx-asr

| Что | Источник | Пометка |
|---|---|---|
| **WER ru** — Russian LibriSpeech *test* | [comparison.md](https://github.com/istupakov/onnx-asr/blob/main/docs/comparison.md) | Считан на **fp32**-весах (строки `onnx-asr`), CPU Ryzen 7 9800X3D. WER для int8 **нигде не публикуется** |
| **RTFx x64 int8** | [benchmarks.md](https://github.com/istupakov/onnx-asr/blob/main/docs/benchmarks.md) → [страница](https://istupakov.github.io/onnx-asr/benchmarks/) | Столбец `x64 RTFx (int8)`, `CPUExecutionProvider`, тот же 9800X3D |

⚠ **Два разных столбца RTFx.** В `tech-models-claude.md` A1 часть чисел взята из comparison.md (это **fp32**), а не из int8-столбца. Ниже — везде int8, как требует §7.1.
⚠ **Три модели каталога вообще отсутствуют в int8-таблице**: GigaAM Multilingual CTC, Multilingual Large CTC, T-one (у T-one int8 явно `N/A` — int8-весов не существует).

## 1. Таблица-источник истины (12 позиций)

Размер = сумма **точных байт** файлов, нужных рантайму (int8-веса + словарь/конфиг), из `GET /api/models/{repo}?blobs=true`.
Ревизия = `sha` ветки `main` на 2026-09-08.

| # | Модель | Репозиторий (рекомендуемый) | Ревизия `main` | Файлы (int8) | Байт | **МБ** | WER ru | RTFx int8 | Пункт. | Языки | Лицензия | Автор / происх. | Увер. |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | GigaAM v3 E2E RNN-T | `csukuangfj/sherpa-onnx-nemo-transducer-punct-giga-am-v3-russian-2025-12-16` | `a6039be7cee829a9044a69ac0ebaf1c191217c97` | encoder.int8 + decoder + joiner + tokens | 231 897 202 | **231,9** | **7,60 %** | **42,5** | да | ru | [MIT](https://huggingface.co/csukuangfj/sherpa-onnx-nemo-transducer-punct-giga-am-v3-russian-2025-12-16/blob/main/LICENSE) «GigaChat Team, 2024» | Сбер (ПАО Сбербанк, РФ) — **отеч.** | high |
| 1a | ↳ вариант под onnx-asr | `istupakov/gigaam-v3-onnx` (`v3_e2e_rnnt_*`) | `322c3b29492673eb7d0b434bfa9dfb8653e34d02` | enc+dec+joint int8 + vocab + yaml | 226 431 968 | **226,4** | те же | те же | да | ru | [MIT](https://huggingface.co/istupakov/gigaam-v3-onnx/blob/main/LICENSE.txt) | Сбер — отеч. | high |
| 2 | GigaAM v3 E2E CTC | `csukuangfj/sherpa-onnx-nemo-ctc-punct-giga-am-v3-russian-2025-12-16` | `4fb5407ff028a69fec516cdf4c10fac9ddea7c16` | model.int8 + tokens | 224 895 668 | **224,9** | **7,80 %** | **52,2** | да | ru | MIT (LICENSE в репо) | Сбер — отеч. | high |
| 3 | GigaAM v3 RNN-T (без пункт.) | `csukuangfj/sherpa-onnx-nemo-transducer-giga-am-v3-russian-2025-12-16` | `0424d62637b37d8b7b1eb0f75a04d78dec55fdcc` | encoder.int8 + decoder + joiner + tokens | 229 343 109 | **229,3** | **4,39 %** | **42,8** | нет | ru | MIT (LICENSE в репо) | Сбер — отеч. | high |
| 4 | GigaAM Multilingual CTC 220M | `istupakov/gigaam-multilingual-ctc-onnx` | `458860e1983aef670dd9795fb6af603c82767d5d` | multilingual_ctc.int8 + vocab + yaml + config | 224 763 921 | **224,8** | **8,43 %** | ✖ нет int8-замера (fp32 = 58,5) | нет | ru, kk, ky, uz, en | [MIT](https://huggingface.co/istupakov/gigaam-multilingual-ctc-onnx) (тег `license:mit`) | Сбер — отеч. | high / RTFx **low** |
| 5 | T-one | `t-tech/T-one` | `106f3b0b32a9e107eb613312e4ebc61ff3d53926` | **model.onnx (fp32!)** + vocab + config | 144 196 802 | **144,2** | **6,57 %** | ✖ int8 = `N/A` (fp32 = 26,3) | нет | ru | [Apache-2.0](https://huggingface.co/t-tech/T-one) (тег) | Т-Банк / T-Tech (АО «ТБанк», РФ) — **отеч.** | high |
| 6 | Vosk ru 0.54 | `alphacep/vosk-model-ru` | `df6a54a4d8e5d43e82675e4f5dba2d507731a0d1` | am-onnx/{encoder,decoder,joiner}.int8 + lang/tokens.txt | 72 468 733 | **72,5** | **9,89 %** | **70,5** | нет | ru | [Apache-2.0](https://huggingface.co/alphacep/vosk-model-ru) (тег) | Alpha Cephei Inc — **юрлицо США (DE)**, команда РФ | high / происх. **med** |
| 7 | Vosk small ru 0.52 | `alphacep/vosk-model-small-ru` | `4d68c4017bcfa44e2a79581f7933339e916a35da` | am/{encoder,decoder,joiner}.int8 + lang/tokens.txt | 26 664 874 | **26,7** | **14,53 %** | **83,5** | нет | ru | [Apache-2.0](https://huggingface.co/alphacep/vosk-model-small-ru) (тег) | Alpha Cephei Inc — см. §3 | high / происх. **med** |
| 8 | Whisper large-v3-turbo | `onnx-community/whisper-large-v3-turbo` (это и бенчмарчено) | `360ebcde2559d60bb474678be3c1de9ef347d01a` | encoder_model_int8 + decoder_model_merged_int8 + токенайзер | 1 089 147 766 | **1 089,1** | **10,10 %** | **3,9** | да | 99 | **MIT** (по [openai/whisper-large-v3-turbo](https://huggingface.co/openai/whisper-large-v3-turbo)) | OpenAI (США) — зарубеж. | high |
| 8a | ↳ вариант sherpa-onnx | `csukuangfj/sherpa-onnx-whisper-turbo` | `2ca6ff69fc878651b770880507669577ac41c2ff` | turbo-encoder.int8 + turbo-decoder.int8 + tokens | 1 036 613 791 | **1 036,6** | (тот же вес) | не бенчмарчено | да | 99 | лицензии в репо **нет** | OpenAI — зарубеж. | high / лиц. **low** |
| 9 | **Whisper small** | `onnx-community/whisper-small` | `36050c46d777d46dc4b5f43f6d90574fc38f8732` | encoder_model_int8 + decoder_model_merged_int8 + токенайзер | 253 466 497 | **253,5** | **нет по протоколу** (см. §2) | **нет по протоколу** (см. §2) | да | 99 | **Apache-2.0** (по [openai/whisper-small](https://huggingface.co/openai/whisper-small)) | OpenAI — зарубеж. | размер high / цифры **low** |
| 9a | ↳ вариант sherpa-onnx | `csukuangfj/sherpa-onnx-whisper-small` | `8f3c18b358db4d1f2fc1eae49d75cd20989e4309` | small-encoder.int8 + small-decoder.int8 + small-tokens | 375 485 327 | **375,5** | — | — | да | 99 | лицензии в репо нет | OpenAI — зарубеж. | high / лиц. low |
| 10 | Whisper base | `istupakov/whisper-base-onnx` (**это и бенчмарчено**, `whisper-ort`) | `998334d3bfe2deba3c8e6821f05388dbf2b706d2` | whisper-base_beamsearch.int8 + токенайзер | 109 069 220 | **109,1** | **38,33 %** | **51,6** | да | 99 | [Apache-2.0](https://huggingface.co/istupakov/whisper-base-onnx) (тег; совпадает с openai/whisper-base) | OpenAI — зарубеж. | high |
| 10a | ↳ вариант sherpa-onnx | `csukuangfj/sherpa-onnx-whisper-base` | `bb53ee204431c90d314c1cc08d28d23e5b7927cc` | base-encoder.int8 + base-decoder.int8 + tokens | 160 609 290 | **160,6** | — | — | да | 99 | лицензии в репо нет | OpenAI — зарубеж. | high / лиц. low |
| 11 | GigaAM Multilingual Large CTC | `istupakov/gigaam-multilingual-large-ctc-onnx` | `07665ab5e54371dd1ac7b8b10f06478003723573` | multilingual_large_ctc.int8 + vocab + yaml + config | 591 646 507 | **591,6** | **5,55 %** | ✖ нет int8-замера (fp32 = 30,4) | нет | ru, kk, ky, uz, en | [MIT](https://huggingface.co/istupakov/gigaam-multilingual-large-ctc-onnx) (тег) | Сбер — отеч. | high / RTFx **low** |
| 12 | NeMo FastConformer ru pc (CTC) | `istupakov/stt_ru_fastconformer_hybrid_large_pc_onnx` | `fb6753736c56de3259d78a26a488548c84b9dccb` | model.int8 + vocab.txt + config | 131 577 107 | **131,6** | **13,10 %** | **70,6** | да | ru | [CC-BY-4.0](https://huggingface.co/nvidia/stt_ru_fastconformer_hybrid_large_pc) — **атрибуция обязательна** | NVIDIA (США) — зарубеж. | high |
| 12a | ↳ тот же репо, RNN-T-декодер | там же | там же | encoder-model.int8 + decoder_joint-model.int8 + vocab | 136 402 626 | **136,4** | 11,57 % | 54,2 | да | ru | CC-BY-4.0 | NVIDIA — зарубеж. | high |

Проверочная арифметика (главные слагаемые, байты):
`#1` 224 570 820 + 4 600 132 + 2 712 896 + 13 354 · `#5` 144 193 371 + 1 821 + 1 054 + 415 + 96 + 45 · `#6` 70 876 638 + 1 326 290 + 259 417 + 6 388 · `#9` 92 326 127 + 156 750 845 + 4 389 525 (токенайзер).

> **Поправка по спайку S3 (2026-09-09):** строки 2–3 таблицы §1 описывают sherpa-экспорты `csukuangfj/*`; для манифеста v1 (рантайм onnx-asr, репо `istupakov/gigaam-v3-onnx`, ревизия `322c3b29492673eb7d0b434bfa9dfb8653e34d02`) точные размеры наборов: e2e_rnnt **226 431 968 Б**, e2e_ctc **224 896 388 Б**, rnnt **225 780 697 Б**; sha256 всех 16 файлов — `arch/spikes/S3.md`.

## 2. Whisper small — отдельно (в первичном ресёрче цифр не было)

| Метрика | Значение | Источник | Пометка |
|---|---|---|---|
| WER ru, **Russian LibriSpeech, int8, onnx-asr** | **данных нет** | — | Whisper small отсутствует и в benchmarks.md, и в comparison.md |
| WER ru, FLEURS (fp16, оригинальный PyTorch) | **11,4 %** | Whisper paper, Табл. 13 «WER (%) on Fleurs», [arXiv:2212.04356](https://arxiv.org/abs/2212.04356) | **другой протокол** · high |
| WER ru, Common Voice 9 | **15,0 %** | там же, Табл. 11 | другой протокол · high |
| Для калибровки: base по тому же протоколу | FLEURS 20,5 % · CV9 28,8 % | там же | а по нашему протоколу base = **38,33 %** |
| **Пересчёт на наш протокол** | **≈ 20–21 % WER** | оценка: 11,4 × (38,33/20,5) = 21,3; 15,0 × (38,33/28,8) = 20,0 | **оценка, не факт** · low |
| RTFx int8 x64 | **данных нет**; оценка **≈ 13–16** | по параметрам: base 74M → 51,6; small 244M (×3,3); turbo 809M → 3,9 | **оценка** · low |

**Рекомендация:** Whisper small — единственная позиция каталога без цифр по протоколу. Либо замерить самим (`onnx-asr` умеет `onnx-community/whisper-small`, датасет `istupakov/russian_librispeech` открыт), либо показывать полоски как «нет данных» — по §7.1 выдумывать нельзя. Оценка 20–21 % ставит small между turbo (10,1) и base (38,3) — правдоподобно, но это интерполяция.

## 3. Alpha Cephei — российская ли компания (вопрос из ТЗ)

| Факт | Источник | Увер. |
|---|---|---|
| Юрлицо называется **«Alpha Cephei Inc.»** — форма `Inc.`, т.е. корпорация США | футер [alphacephei.com](https://alphacephei.com/en/) «2015–2025 Alpha Cephei Inc.»; профиль HF-организации [alphacep](https://huggingface.co/alphacep) | high |
| Адрес в бизнес-справочниках — **Lewes, Delaware, США** | [D&B business directory](https://www.dnb.com/business-directory/company-profiles.alpha_cephei_inc.dc5e2c42c640a81b8d627d07275f9a5f.html) (сама страница отдала 403 нашему запросу, взято из выдачи), [ZoomInfo](https://www.zoominfo.com/c/alpha-cephei-inc/460312140) | **med** |
| Основатель/CEO — **Николай Шмырёв**, команда и профили указывают Россию (Москва / Астрахань) | [Tracxn](https://tracxn.com/d/companies/alpha-cephei/__0tE60sIOgoM34VaM7MRQrDGdy0mr_ftrw834WzTP1ZA), [github.com/nshmyrev](https://github.com/nshmyrev) | med |
| Российского юрлица (ООО) не найдено; на сайте нет ни реквизитов, ни страны | alphacephei.com — только e-mail contact@ | med (отсутствие доказательства ≠ доказательство отсутствия) |

**Вывод для профиля «только отечественные»:** по **юрлицу** Vosk — **не отечественная** (US Inc., Delaware); по **команде** — российская разработка. Формулировка макета «происхождение уточняется» для 0.54 верна, но у Vosk small 0.52 в макете стоит «отечественная» — это **противоречие внутри одного макета**. Решение (юрлицо или команда) — за человеком; в реестре отечественного ПО Vosk нет (не проверялось отдельно — пробел данных).

## 4. Расхождения с PRD §7.2 и макетом (было → стало)

### 4.1 Размер на диске (МиБ вместо МБ — системная ошибка)

| Модель | PRD/макет | По HF API | Δ |
|---|---|---|---|
| GigaAM Multilingual CTC 220M | 214 МБ | **224,8 МБ** | +10,8 |
| T-one | 138 МБ | **144,2 МБ** (и это **fp32**, int8 не существует) | +6,2 |
| Vosk ru 0.54 | ~70 МБ | **72,5 МБ** | +2,5 |
| Vosk small ru 0.52 | ~26 МБ | **26,7 МБ** | +0,7 |
| Whisper large-v3-turbo | 987 МБ | **1 036,6 МБ** (sherpa) / **1 089,1 МБ** (onnx-community) | +50…+102 |
| Whisper small | 357 МБ | **375,5 МБ** (sherpa) / **253,5 МБ** (onnx-community) | +18,5 / −103,5 |
| Whisper base | 153 МБ | **160,6 МБ** (sherpa) / **109,1 МБ** (istupakov, он и бенчмарчен) | +7,6 / −43,9 |
| GigaAM Multilingual Large CTC | 564 МБ | **591,6 МБ** | +27,6 |
| NeMo FastConformer ru pc | 125 МБ | **131,6 МБ** (CTC) / 136,4 (RNN-T) | +6,6 |
| GigaAM v3 ×3 | 232 / 225 / 230 МБ | **231,9 / 224,9 / 229,3** | ✓ верно |
| Итого «Установлено 3 из 12 · 687 МБ» (макет) | 687 МБ | **686,1 МБ** (231,9+224,9+229,3) | ✓ округление ок |

### 4.2 WER — макет vs протокол

| Модель | Макет | По протоколу | Комментарий |
|---|---|---|---|
| GigaAM v3 RNN-T (e2e) | 7,6 % | **7,60 %** | ✓ |
| GigaAM v3 CTC (e2e) | 8,3 % | **7,80 %** | заглушка |
| GigaAM v3 RNN-T без пункт. | 7,4 % | **4,39 %** | реальность **лучше** |
| GigaAM Multilingual CTC 220M | 10,1 % | **8,43 %** | лучше |
| T-one | 11,5 % | **6,57 %** | сильно лучше |
| Vosk ru 0.54 | 18,4 % | **9,89 %** | сильно лучше |
| Vosk small ru 0.52 | 22,1 % | **14,53 %** | лучше |
| Whisper large-v3-turbo | 13,0 % | **10,10 %** | лучше |
| Whisper small | 16,9 % | **нет данных** (оценка ≈20–21 %) | цифра выдумана |
| Whisper base | 27,8 % | **38,33 %** | реальность **хуже**; подпись «ошибка почти в каждом четвёртом слове» → **почти в каждом третьем** |
| GigaAM Multilingual Large CTC | 6,9 % | **5,55 %** | лучше |
| NeMo FastConformer ru pc | 15,2 % | **13,10 %** | лучше |

### 4.3 Скорость — макет vs протокол (RTFx x64 int8)

| Модель | Макет | int8 (протокол) | Комментарий |
|---|---|---|---|
| GigaAM v3 RNN-T e2e | ≈42× | **42,5** | ✓ |
| GigaAM v3 CTC e2e | ≈53× | **52,2** | ✓ |
| GigaAM v3 RNN-T | ≈42× | **42,8** | ✓ |
| GigaAM Multilingual CTC 220M | ≈38× | **нет int8**; fp32 58,5 | заглушка + пробел данных |
| T-one | ≈50× | **int8 N/A**; fp32 26,3 | вдвое завышено |
| Vosk ru 0.54 | ≈52× | **70,5** | занижено (A1 давал 62,3 — это fp32) |
| Vosk small ru 0.52 | ≈55× | **83,5** | занижено (A1 давал 72,6 — fp32) |
| Whisper large-v3-turbo | ≈1,8× | **3,9** | занижено |
| Whisper small | ≈4,1× | **нет данных** (оценка ≈13–16) | цифра выдумана |
| Whisper base | ≈9× | **51,6** | занижено в 5,7 раза |
| GigaAM Multilingual Large CTC | ≈11× | **нет int8**; fp32 30,4 | заглушка + пробел |
| NeMo FastConformer ru pc | ≈58× | **70,6** (CTC int8) | A1 давал 96,1 — это fp32 |

### 4.4 Прочее

| Что | Было | Стало |
|---|---|---|
| Лицензия Whisper | «MIT / Apache-2.0 (обе в NOTICE)» на всех трёх | **turbo = MIT**, **small = Apache-2.0**, **base = Apache-2.0** (по карточкам `openai/*`) |
| Репозиторий Whisper base | csukuangfj (sherpa) | Бенчмарк снят с **`istupakov/whisper-base-onnx`** (`whisper-ort`), 109 МБ — только он подтверждён цифрами |
| Whisper small в onnx-asr | не оговорено | Поддержан **только** как `onnx-community/whisper-small` (optimum-экспорт); отдельного istupakov-репо нет |
| «Происхождение» Vosk | 0.54 «уточняется», 0.52 «отечественная» | Одинаковое для обеих; по юрлицу — **US Inc.** (§3) |
| Пример манифеста PRD §7.3 | `engine: onnx-asr` + repo csukuangfj | Несовместимо: onnx-asr читает istupakov-раскладку (`v3_e2e_rnnt_encoder.int8.onnx`), sherpa — csukuangfj. Выбрать одно |

## 5. Противоречия и пробелы

- **RTFx: fp32 vs int8.** В `tech-models-claude.md` A1 смешаны оба столбца. Для 6 моделей из 12 числа в A1 — fp32.
- **WER считан на fp32, а в каталоге стоит int8.** Публичного WER для int8 нет ни у кого. Полоска качества по §7.1 честна только с оговоркой «WER измерен на fp32-весах той же модели».
- **T-one без int8.** Модель в каталоге пойдёт как fp32 144 МБ; ОЗУ будет заметно выше оценки «×1,85 от int8».
- **GigaAM Multilingual (обе) без int8-замера скорости** — полоску скорости честно взять неоткуда.
- **csukuangfj-репозитории Whisper без файла лицензии** (теги `onnx, region:us`, cardData пуст) — юридически опираться нужно на `openai/whisper-*`.
- **ОЗУ:** замер по-прежнему один (415 МБ на позиции 1). Правило «×1,85» выведено из одной точки и к fp32-T-one неприменимо.
- **Whisper small** — ни WER, ни RTFx по протоколу (§2).
- Не проверялось: наличие Vosk/GigaAM в реестре отечественного ПО; лицензионный статус `kenlm.bin` T-one (5,46 ГБ, нам не нужен).
