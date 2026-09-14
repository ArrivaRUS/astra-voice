# Пустой текст GigaAM v3 int8 на Intel Xeon с AMX — разбор (Researcher, 2026-09-14)

**Симптом.** В CI (GitHub-hosted, debian:12) на раннерах **Intel Xeon Platinum 8573C** (amx_int8/amx_tile, avx512_vnni, avx_vnni)
движок возвращает `text=''` при нормальном `infer_ms≈200` на 6-секундном WAV, без исключений (прогоны `732a116`, `e117e1b`,
`6f335f9`). На **AMD EPYC 7763** (только avx2) и на машине заказчика **Core Ultra 7 255U** (avx2 + avx_vnni) — верный текст.

**Причина (подтверждена по исходникам).** Дефект MLAS в onnxruntime ≤ 1.24.4, исправлен PR
[microsoft/onnxruntime#27671](https://github.com/microsoft/onnxruntime/pull/27671) (влит 2026-03-18), в релизе
[v1.25.0](https://github.com/microsoft/onnxruntime/releases/tag/v1.25.0) (2026-04-20). **v1.24.4 вышел 2026-03-17 — за сутки
до фикса** и является последним релизом ветки 1.24.x.
- Модель `gigaam-v3-onnx` e2e_rnnt int8 квантована по схеме **u8u8** (скан локальных файлов: энкодер 128× `MatMulInteger`,
  50× `ConvInteger`, 162× `DynamicQuantizeLinear`, все 356 квантованных инициализаторов UINT8; джойнт — 3× `MatMulInteger`;
  декодер — `DynamicQuantizeLSTM` с INT8-весами).
- `matmul_integer.cc:85,104` выводит знаковость из dtype → пара (false,false) → `qgemm.h:876-877` берёт `GemmU8U8Dispatch`.
- `platform.cpp` v1.24.4, строка 543: на CPU с AMX-TILE+AMX-INT8 `GemmU8U8Dispatch = &MlasGemmU8S8DispatchAmx` — u8u8-нагрузка
  уезжает в u8s8-диспатч AMX (первопричина №1 по PR #27671); там же починен AMX-путь `CountM ≥ 32` с неверными аккумуляторами
  (первопричина №3; у нас M≈150). В v1.23.2/v1.24.4 таких присваиваний три, в v1.25.0…v1.30.0 — ноль.
- AVX-512-VNNI в 1.24.4 подменяет только `GemmU8S8Kernel` — на u8u8-модель не влияет; подозрение с VNNI снято.
- Почему пустая строка, а не мусор: испорченные логиты джойнта → argmax = blank на каждом шаге greedy-декодера RNN-T →
  штатные ~200 мс и пустой результат (интерпретация, не источник).

**Что не работает.** Ручки отключения AMX нет: в `platform.cpp` v1.24.4 нет ни одного `getenv`; единственный селектор
бэкенда MLAS — `mlas.disable_kleidiai` (ARM, с 1.25.0); build-флаг `--use_amx` убран в 2023 (PR #16086/#16527).
Перепаковка u8u8 → u8s8 на 1.24.4 бессмысленна (строка 544 уводит u8s8 туда же); `DynamicQuantizeMatMul` ↔
`MatMulIntegerToFloat` — тот же `MlasGemmBatch`. Теоретический рычаг без пересборки — seccomp-запрет
`arch_prctl(ARCH_REQ_XCOMP_PERM, XTILEDATA)` (тогда `MlasInitAMX()` вернёт false); применений не найдено (уверенность med).
Проверка выбранного пути: `strace -f -e trace=arch_prctl` → `arch_prctl(0x1023, 0x12) = 0`.

**Решение для продукта.** `onnxruntime >= 1.25.0` в `packaging/wheels.lock` (прямой фикс). Запасные пути: fp32-веса
(`v3_e2e_rnnt_*.onnx` в том же HF-репо) или откат на 1.20.x (сторонний репорт #26324: 1.20.1 чист, 1.23.1 нет).
Нижнюю границу версии зафиксировать в `decisions/log.md` с причиной, чтобы никто не «откатил для стабильности».

**Для CI.** Тип CPU на GitHub-hosted раннерах выбрать нельзя (документация не раскрывает модель; парк смешанный:
Xeon 8370C/8573C и EPYC 7763, назначение случайное). Правильный путь — корректный код на любом CPU + ассерт на непустой текст
остаётся как регресс-детектор (он и поймал дефект). Детерминированное железо — только self-hosted/сторонние раннеры.
Примечательно: AMX и AVX-VNNI-INT8 ядра в CI самого onnxruntime не исполняются никогда (issue #29862) — поэтому дефект
прожил долго.

**Пробелы.** `DynamicQuantizeLSTM` (декодер) на AMX-путях отдельно не проверялся; per-channel не парсился; отсутствие issue у
onnx-asr/sherpa-onnx/GigaAM — не доказательство отсутствия проблемы (0 discussions у модели на HF).

**Ссылки.** PR #27671 · issue #27670 · issue #26324 · issue #29862 / PR #29903 · PR #27136 · PR #16086, #16527 ·
release v1.25.0 · `platform.cpp`/`qgemm.h`/`matmul_integer.cc` @ v1.24.4 · HF `istupakov/gigaam-v3-onnx` ·
GitHub Docs «GitHub-hosted runners» · runs-on.com benchmarks.
