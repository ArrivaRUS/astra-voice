# Источники и проверка переноса Astra Voice

Снимок подготовлен 5 октября 2026 года. Локальные исходники прочитаны без изменения. Проверены наличие и читаемость документов, структура пакета и контрольные суммы. Тесты продукта не запускались, текущие PR и облачные чаты не проверялись.

Исходный репозиторий: `/home/astra/Документы/astra-voice`.

Общая память: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/memory/`. Исходный общий индекс разделён по проектам; файл со ссылкой на агентский GitHub-токен исключён. Не относящиеся к проектам чаты (обновление Claude, OnlyOffice, общая настройка панели, ClaudeCodexLimits, тестовые «ok») не переносились. Полные старые копии документов из worktree не дублировались; добавлены только отличающиеся изменения документов веток относительно их точки ответвления.

## Удалённые значения доступа

Значения, похожие на секреты, заменены на `[REDACTED_SECRET]`. Проверка по шаблонам; это не формальный аудит всех документов.

- [project/docs/test-plan.md](project/docs/test-plan.md) — url-credentials, замен: 3.
- [project/docs/threat-model.md](project/docs/threat-model.md) — url-credentials, замен: 2.

## Уточнение перед коммитом 12 октября 2026 года

В `chat-history/summaries/2026-09-17_46e5d286_04.md` дословный пример тестового пароля заменён на `[ТЕСТОВАЯ СТРОКА СКРЫТА]`: он вызывал ложное срабатывание pre-commit проверки секретов. Смысл исторического отчёта сохранён; SHA256 копии ниже обновлён. Исходная контрольная сумма копии до этой правки: `9e62a5ecee3fa828203258e81d0a78a83460fd055104b9116a97b105308ca6b7`. Остальные архивные файлы не переформатировались.

## Происхождение документов

### branch-notes/wt-micvol/PRD.md

[Открыть копию](branch-notes/wt-micvol/PRD.md)

Источник: `/home/astra/.cache/astra-voice-dev/wt-micvol/PRD.md`

SHA256 копии: `9a53cfce4c196b1a7885d470f56f1bb932276dc121ab481a37cc35622fe0189f`

### branch-notes/wt-micvol/docs/ui-bridge.md

[Открыть копию](branch-notes/wt-micvol/docs/ui-bridge.md)

Источник: `/home/astra/.cache/astra-voice-dev/wt-micvol/docs/ui-bridge.md`

SHA256 копии: `2d66439e6eba8cb2a34644562fa6f726d9c94527954cb43ec4a8aaff77a33e1b`

### branch-notes/wt-rv-mic/docs/ui-bridge.md

[Открыть копию](branch-notes/wt-rv-mic/docs/ui-bridge.md)

Источник: `/home/astra/.cache/astra-voice-dev/wt-rv-mic/docs/ui-bridge.md`

SHA256 копии: `592c0f644382e514188f8bcbdf93a5066882865f6806dde00c5b36523c59c3b5`

### chat-history/dialogue/2026-08-27_ba215ac1.md

[Открыть копию](chat-history/dialogue/2026-08-27_ba215ac1.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/ba215ac1-d49c-4bb0-8037-0834b2365f84.jsonl`

SHA256 копии: `6bd90b43421183bb71d05d22d44cd2051d5b9ee8412836b7c0cdadf31265f495`

### chat-history/dialogue/2026-09-01_f80945d6.md

[Открыть копию](chat-history/dialogue/2026-09-01_f80945d6.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/f80945d6-d939-49ae-ad46-6564627e0c6b.jsonl`

SHA256 копии: `923c2efcaed0c9f7bbf14ab4d498835d005ae5353730be6578220668e9a03ee5`

### chat-history/dialogue/2026-09-02_f80945d6.md

[Открыть копию](chat-history/dialogue/2026-09-02_f80945d6.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/f80945d6-d939-49ae-ad46-6564627e0c6b.jsonl`

SHA256 копии: `8546a4e22a3819a2e19ca33af31862f497add2e056b64683a4e26dd8f21fb887`

### chat-history/dialogue/2026-09-04_f80945d6.md

[Открыть копию](chat-history/dialogue/2026-09-04_f80945d6.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/f80945d6-d939-49ae-ad46-6564627e0c6b.jsonl`

SHA256 копии: `ed6708634d65d6797d38542407cf9a154948a28f77d8eee88f00befcf6dfe227`

### chat-history/dialogue/2026-09-07_46e5d286.md

[Открыть копию](chat-history/dialogue/2026-09-07_46e5d286.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `2638f86ac08d3149dc8d4d71bb161d2aaebcd53fe9b06a912fef078981945ffb`

### chat-history/dialogue/2026-09-08_46e5d286.md

[Открыть копию](chat-history/dialogue/2026-09-08_46e5d286.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `a732a8ad292c1099db843f6b99b320a373dd17011ad54e23144a7600bb8681fb`

### chat-history/dialogue/2026-09-09_46e5d286.md

[Открыть копию](chat-history/dialogue/2026-09-09_46e5d286.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `26199f7a1dc0ba47eb57595625f43031611d39bdbb4bb072f5c605e2b2a512c0`

### chat-history/dialogue/2026-09-11_46e5d286.md

[Открыть копию](chat-history/dialogue/2026-09-11_46e5d286.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `a414e907ab0f88b3b2af450fde74be218eea3823dcc13b51617bb8131059d092`

### chat-history/dialogue/2026-09-14_46e5d286.md

[Открыть копию](chat-history/dialogue/2026-09-14_46e5d286.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `2bb4f70167b272f4087b8b8849715b6e592f8ce41797d9af9f8af8ce2545c2ed`

### chat-history/dialogue/2026-09-15_46e5d286.md

[Открыть копию](chat-history/dialogue/2026-09-15_46e5d286.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `2f61a655bdf3f57a36d12b45c779bba33910dab463f06119d4df7a4521a6ad9b`

### chat-history/dialogue/2026-09-16_46e5d286.md

[Открыть копию](chat-history/dialogue/2026-09-16_46e5d286.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `f462b3aa754a30db7b6eaac3631c972eca0e203e2dd904c891a5a5b77a3dcb10`

### chat-history/dialogue/2026-09-17_46e5d286.md

[Открыть копию](chat-history/dialogue/2026-09-17_46e5d286.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `a92ba10d071b6263126fb5ba4fd090ff71b81cd11f96b03d6527aca5bef6e73d`

### chat-history/dialogue/2026-09-21_46e5d286.md

[Открыть копию](chat-history/dialogue/2026-09-21_46e5d286.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `ad13bb275fdc6a6197c4ac3d00538e3a1956948404b4b32a93ccc7b7a217d743`

### chat-history/dialogue/2026-09-22_46e5d286.md

[Открыть копию](chat-history/dialogue/2026-09-22_46e5d286.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `04ef75dade8136ec3839e2b87905a4890e6846788b964dc436818a943062c15a`

### chat-history/dialogue/2026-09-23_46e5d286.md

[Открыть копию](chat-history/dialogue/2026-09-23_46e5d286.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `6b9e22c388ce1b35f352b8f64940af1e4c5cc6c1b30a0a7c48ebf7db87833d26`

### chat-history/dialogue/2026-09-24_46e5d286.md

[Открыть копию](chat-history/dialogue/2026-09-24_46e5d286.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `9f9e35183cc6274b05e92decb18dd070e46ee7e90e07979606ff088bb9f623eb`

### chat-history/dialogue/2026-09-26_46e5d286.md

[Открыть копию](chat-history/dialogue/2026-09-26_46e5d286.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `52cfac6257159bbceccf10745af88228704138eadcfef6d4829e24625545e157`

### chat-history/dialogue/2026-09-27_46e5d286.md

[Открыть копию](chat-history/dialogue/2026-09-27_46e5d286.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `34852d9b548a75694692b444d3189be223c8e145461a75cac7af8c2361d62ee3`

### chat-history/dialogue/2026-09-28_46e5d286.md

[Открыть копию](chat-history/dialogue/2026-09-28_46e5d286.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `b104b5bd40a0d2368c913d1eaa8caa1357e728b71f9436b01091b036953757b5`

### chat-history/dialogue/2026-09-29_46e5d286.md

[Открыть копию](chat-history/dialogue/2026-09-29_46e5d286.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `2c993486bc7f806375873157699e5a627f3e86c0a890f5a36e21b9d4003b793a`

### chat-history/dialogue/2026-09-30_46e5d286.md

[Открыть копию](chat-history/dialogue/2026-09-30_46e5d286.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `b3820da2cda7dcefa297c63c9d1e5509a565675783d45843fa5cf86de8afa7f2`

### chat-history/summaries/2026-09-09_46e5d286_01.md

[Открыть копию](chat-history/summaries/2026-09-09_46e5d286_01.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `5e87245b50833683b8c9fd900cd5a96703e93d5ea09daa91cc6e5a0f3fbb1992`

### chat-history/summaries/2026-09-14_46e5d286_02.md

[Открыть копию](chat-history/summaries/2026-09-14_46e5d286_02.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `7fa4912a378fab3481ff98d801d63d83887c751115144c73dbdfbb8eb7308181`

### chat-history/summaries/2026-09-15_46e5d286_03.md

[Открыть копию](chat-history/summaries/2026-09-15_46e5d286_03.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `3fa63a1d2057d6ee72c012935dae5d3c75d6c7baa43911925036a4080e5e9f70`

### chat-history/summaries/2026-09-17_46e5d286_04.md

[Открыть копию](chat-history/summaries/2026-09-17_46e5d286_04.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `d976d4c4bec5a676b117168d558be67d7539e576af0cef0b4c8919532f6efd18`

### chat-history/summaries/2026-09-22_46e5d286_05.md

[Открыть копию](chat-history/summaries/2026-09-22_46e5d286_05.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `2b5ff15adb6ec03743a6e5c023f88aeb4431fe982396891f58efd017b3fe0faa`

### chat-history/summaries/2026-09-22_46e5d286_06.md

[Открыть копию](chat-history/summaries/2026-09-22_46e5d286_06.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `79a375ff040681ee2f5c9775e9245f5b0a120d8d9d4b0c84d166db37afe9432a`

### chat-history/summaries/2026-09-23_46e5d286_07.md

[Открыть копию](chat-history/summaries/2026-09-23_46e5d286_07.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `25c6833caa0d00550b8190d457f84b62048e6741d318e566b353914b87346ac5`

### chat-history/summaries/2026-09-26_46e5d286_08.md

[Открыть копию](chat-history/summaries/2026-09-26_46e5d286_08.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `45387ce3c22cae73233b09538d3d78e5188772127a9c20d9cf57166d1d92a20c`

### chat-history/summaries/2026-09-27_46e5d286_09.md

[Открыть копию](chat-history/summaries/2026-09-27_46e5d286_09.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `3e090e3f8bc907ebb6a3431ae7f5a569a66725052d762b182b5ae14a30d71159`

### chat-history/summaries/2026-09-28_46e5d286_10.md

[Открыть копию](chat-history/summaries/2026-09-28_46e5d286_10.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `46c41e669e10b9323fa629151ec434f1562eb13889a44c7c30d3b888b31b505d`

### chat-history/summaries/2026-09-28_46e5d286_11.md

[Открыть копию](chat-history/summaries/2026-09-28_46e5d286_11.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `aa1455207ae238872ce3ef9e5e0956114a6a244c5b0900c0d3592b2b553b0a19`

### chat-history/summaries/2026-09-29_46e5d286_12.md

[Открыть копию](chat-history/summaries/2026-09-29_46e5d286_12.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `0edd775f0407f35002473e7ab9d061cc9b9aac0e1d822ca6e0fa3696fa8a9c7c`

### chat-history/summaries/2026-09-29_46e5d286_13.md

[Открыть копию](chat-history/summaries/2026-09-29_46e5d286_13.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `aa63210606644905cdac08446c2e795506a690c0149b3feb231b6d4f1aafbe70`

### chat-history/summaries/2026-09-30_46e5d286_14.md

[Открыть копию](chat-history/summaries/2026-09-30_46e5d286_14.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `d4401a61fc84d649abfbd49397a552dc05d68dae43ea018c2a60222a9112764a`

### chat-history/summaries/2026-09-30_46e5d286_15.md

[Открыть копию](chat-history/summaries/2026-09-30_46e5d286_15.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/46e5d286-3670-4c05-b2d6-7d8544c21f2d.jsonl`

SHA256 копии: `20f559d53d5772ca4cb89e2e2ec39067ab0e3f26666dc28c7e269ef7854c94af`

### claude-memory/agent_worktree_needs_repo_cwd.md

[Открыть копию](claude-memory/agent_worktree_needs_repo_cwd.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/memory/agent_worktree_needs_repo_cwd.md`

SHA256 копии: `7f31f64095e29c4cec0088677952a826ee52048fceb0ed3b9203a775ef7061d6`

### claude-memory/astra_kde_fly_notifications_kills_plasmashell.md

[Открыть копию](claude-memory/astra_kde_fly_notifications_kills_plasmashell.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/memory/astra_kde_fly_notifications_kills_plasmashell.md`

SHA256 копии: `eff2b1177bee8d9e1f770da3acca0351dc389aa54411976d25977eec585aaafe`

### claude-memory/astra_voice_autostart.md

[Открыть копию](claude-memory/astra_voice_autostart.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/memory/astra_voice_autostart.md`

SHA256 копии: `6d948bab4088d93d0e81998f673bdca2d0c189fc8b4db9678fe3f3f54fd6f7cd`

### claude-memory/codex_cli_astra_machine.md

[Открыть копию](claude-memory/codex_cli_astra_machine.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/memory/codex_cli_astra_machine.md`

SHA256 копии: `ec3c0e5521bcb96a2ea27d5522a3d7f587fc737c5350ef0da09f43173b2d1508`

### claude-memory/environment/astra-linux-environment.md

[Открыть копию](claude-memory/environment/astra-linux-environment.md)

Источник: `/home/astra/.claude/projects/-home-astra--config-Claude-scratch-workspaces-96ee42a5-7aec-41fe-aef5-0c37ac78288e-a9538217-8054-4390-94f0-deeec2deded9-scratch-2026-09-09-a7450a/memory/astra-linux-environment.md`

SHA256 копии: `a821188ed244a3a2182805dc220bbeb8355947d8f256d9fae69b834bea65f2fb`

### claude-memory/environment/astra-linux-plasma-environment.md

[Открыть копию](claude-memory/environment/astra-linux-plasma-environment.md)

Источник: `/home/astra/.claude/projects/-home-astra--config-Claude-scratch-workspaces-96ee42a5-7aec-41fe-aef5-0c37ac78288e-a9538217-8054-4390-94f0-deeec2deded9-scratch-2026-09-09-a782a0/memory/astra-linux-plasma-environment.md`

SHA256 копии: `80134e78109f5dbf4f79a67d66c33905c395d379d6f439fc3e2b137631e203b5`

### claude-memory/environment/kde-icon-name-dash-fallback.md

[Открыть копию](claude-memory/environment/kde-icon-name-dash-fallback.md)

Источник: `/home/astra/.claude/projects/-home-astra--config-Claude-scratch-workspaces-96ee42a5-7aec-41fe-aef5-0c37ac78288e-a9538217-8054-4390-94f0-deeec2deded9-scratch-2026-09-09-a782a0/memory/kde-icon-name-dash-fallback.md`

SHA256 копии: `bc6d7bf144ee4b07235281aacfd13491eaf4e170777531b6ed82dab3faa9012a`

### claude-memory/feedback_codex_limits_architecture_only.md

[Открыть копию](claude-memory/feedback_codex_limits_architecture_only.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/memory/feedback_codex_limits_architecture_only.md`

SHA256 копии: `4bb2bee5a2df2f8ba5697a3f6b80c0bbd71eb419ef78abf1aba43e6a33c5f8d9`

### claude-memory/feedback_fewer_security_reviews.md

[Открыть копию](claude-memory/feedback_fewer_security_reviews.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/memory/feedback_fewer_security_reviews.md`

SHA256 копии: `4235efffcff956a6d3a3153d5e199eb5f605c48e45ee2cff0e2661f2da855d4a`

### claude-memory/feedback_no_repeat_security_nags.md

[Открыть копию](claude-memory/feedback_no_repeat_security_nags.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/memory/feedback_no_repeat_security_nags.md`

SHA256 копии: `b07a92570605b552f131f617e0180041242c99e1343a05f5072e158816cedd94`

### claude-memory/feedback_no_security_nagging_beta.md

[Открыть копию](claude-memory/feedback_no_security_nagging_beta.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/memory/feedback_no_security_nagging_beta.md`

SHA256 копии: `a94a8b118b9a14d984c77ea864082ccdb343848c9bde04db1c770a30dede3d8c`

### claude-memory/feedback_no_ui_text_for_timing.md

[Открыть копию](claude-memory/feedback_no_ui_text_for_timing.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/memory/feedback_no_ui_text_for_timing.md`

SHA256 копии: `e80a8ff919fff0969060117dfb49473e6c81bd1ce82ae1eae5fa3890d47c2167`

### claude-memory/feedback_respond_in_russian.md

[Открыть копию](claude-memory/feedback_respond_in_russian.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/memory/feedback_respond_in_russian.md`

SHA256 копии: `99b5bb7107cc07e1df80d114f0e2d7092720bfd16ee50762f5079a4a02f5d57c`

### claude-memory/feedback_security_reviews_sparingly.md

[Открыть копию](claude-memory/feedback_security_reviews_sparingly.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/memory/feedback_security_reviews_sparingly.md`

SHA256 копии: `122b47183635683dd712f768afd695b6d6294858712b4ce8fb1fbfdd015552dc`

### claude-memory/feedback_ui_copy_plain_language.md

[Открыть копию](claude-memory/feedback_ui_copy_plain_language.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/memory/feedback_ui_copy_plain_language.md`

SHA256 копии: `2d56c265d1486c37395deee89a745355d10c11dd246a838d0663b33df43f4339`

### claude-memory/handy_custom_build.md

[Открыть копию](claude-memory/handy_custom_build.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/memory/handy_custom_build.md`

SHA256 копии: `98e5ead09dd1dda7d8edf277bb5112691fc57b7dea0eee21f13def80e744bdfb`

### claude-memory/needrestart_user_session_dialog.md

[Открыть копию](claude-memory/needrestart_user_session_dialog.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/memory/needrestart_user_session_dialog.md`

SHA256 копии: `e93217a82f526327770f837ddab6929220988df693bb414a973813f8da295065`

### claude-memory/project_astra_voice.md

[Открыть копию](claude-memory/project_astra_voice.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/memory/project_astra_voice.md`

SHA256 копии: `6cd754d177e32385c313645632a1c4f6c60e646d7738e3fee3f2ba832be08af9`

### claude-memory/project_astra_voice_dev_docs.md

[Открыть копию](claude-memory/project_astra_voice_dev_docs.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/memory/project_astra_voice_dev_docs.md`

SHA256 копии: `75916f11d530db84421b4364c8afe4952d6cef9e3d819e8bde0b0a85e36ef0fd`

### claude-memory/repo_handy_gigaam.md

[Открыть копию](claude-memory/repo_handy_gigaam.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/memory/repo_handy_gigaam.md`

SHA256 копии: `94adc89b8f512e5229fb9956fd030f325647f0c176f1e6980a32ea1f90b54458`

### claude-memory/stt_engine_gigaam.md

[Открыть копию](claude-memory/stt_engine_gigaam.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/memory/stt_engine_gigaam.md`

SHA256 копии: `3055aa374cca1a2905e5e6d89b26be79fc18177df5bfb0963d61d56473e77819`

### claude-memory/target_environment_astra_1_8.md

[Открыть копию](claude-memory/target_environment_astra_1_8.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/memory/target_environment_astra_1_8.md`

SHA256 копии: `e946df9418f04b3ef61a1a2a6d9ba670211080d4424fcd9eb72539986dbd3e09`

### claude-memory/user_role_astra_cpo.md

[Открыть копию](claude-memory/user_role_astra_cpo.md)

Источник: `/home/astra/.claude/projects/-home-astra-----------Astra-Cowork/memory/user_role_astra_cpo.md`

SHA256 копии: `83a830a9f34bd8a67e20bf6dae7cfcea47e8bb14164f76e3fad04d309bbfe0df`

### project/.patches/001-агенты-пишут-артефакт-в-конце-обрыв-теряет-всё.md

[Открыть копию](project/.patches/001-%D0%B0%D0%B3%D0%B5%D0%BD%D1%82%D1%8B-%D0%BF%D0%B8%D1%88%D1%83%D1%82-%D0%B0%D1%80%D1%82%D0%B5%D1%84%D0%B0%D0%BA%D1%82-%D0%B2-%D0%BA%D0%BE%D0%BD%D1%86%D0%B5-%D0%BE%D0%B1%D1%80%D1%8B%D0%B2-%D1%82%D0%B5%D1%80%D1%8F%D0%B5%D1%82-%D0%B2%D1%81%D1%91.md)

Источник: `/home/astra/Документы/astra-voice/.patches/001-агенты-пишут-артефакт-в-конце-обрыв-теряет-всё.md`

SHA256 копии: `4c3921eb6096061d9e381371abbda1371d424b28f7492886cd16c632fc75e002`

### project/.patches/002-спайк-мигнул-иконкой-на-панели-заказчика-шина-не-дисплей.md

[Открыть копию](project/.patches/002-%D1%81%D0%BF%D0%B0%D0%B9%D0%BA-%D0%BC%D0%B8%D0%B3%D0%BD%D1%83%D0%BB-%D0%B8%D0%BA%D0%BE%D0%BD%D0%BA%D0%BE%D0%B9-%D0%BD%D0%B0-%D0%BF%D0%B0%D0%BD%D0%B5%D0%BB%D0%B8-%D0%B7%D0%B0%D0%BA%D0%B0%D0%B7%D1%87%D0%B8%D0%BA%D0%B0-%D1%88%D0%B8%D0%BD%D0%B0-%D0%BD%D0%B5-%D0%B4%D0%B8%D1%81%D0%BF%D0%BB%D0%B5%D0%B9.md)

Источник: `/home/astra/Документы/astra-voice/.patches/002-спайк-мигнул-иконкой-на-панели-заказчика-шина-не-дисплей.md`

SHA256 копии: `e28a1af0fbf52e846cf895eef5d0a45b2589374564498bfab57e4a899cf3e2c2`

### project/.patches/003-параллельные-агенты-делят-scratchpad-пересеклись-дисплеи.md

[Открыть копию](project/.patches/003-%D0%BF%D0%B0%D1%80%D0%B0%D0%BB%D0%BB%D0%B5%D0%BB%D1%8C%D0%BD%D1%8B%D0%B5-%D0%B0%D0%B3%D0%B5%D0%BD%D1%82%D1%8B-%D0%B4%D0%B5%D0%BB%D1%8F%D1%82-scratchpad-%D0%BF%D0%B5%D1%80%D0%B5%D1%81%D0%B5%D0%BA%D0%BB%D0%B8%D1%81%D1%8C-%D0%B4%D0%B8%D1%81%D0%BF%D0%BB%D0%B5%D0%B8.md)

Источник: `/home/astra/Документы/astra-voice/.patches/003-параллельные-агенты-делят-scratchpad-пересеклись-дисплеи.md`

SHA256 копии: `c2df25118aafc0416591b734b51440359647749a356444e406e7be5725fa9c17`

### project/.patches/004-микрофон-оставался-открытым-между-записями-звук-из-паузы.md

[Открыть копию](project/.patches/004-%D0%BC%D0%B8%D0%BA%D1%80%D0%BE%D1%84%D0%BE%D0%BD-%D0%BE%D1%81%D1%82%D0%B0%D0%B2%D0%B0%D0%BB%D1%81%D1%8F-%D0%BE%D1%82%D0%BA%D1%80%D1%8B%D1%82%D1%8B%D0%BC-%D0%BC%D0%B5%D0%B6%D0%B4%D1%83-%D0%B7%D0%B0%D0%BF%D0%B8%D1%81%D1%8F%D0%BC%D0%B8-%D0%B7%D0%B2%D1%83%D0%BA-%D0%B8%D0%B7-%D0%BF%D0%B0%D1%83%D0%B7%D1%8B.md)

Источник: `/home/astra/Документы/astra-voice/.patches/004-микрофон-оставался-открытым-между-записями-звук-из-паузы.md`

SHA256 копии: `6acd0b6be662cabd8dc215b9378fa3b8a7458264a49a0eedb1e891bb42aa5fa9`

### project/.patches/005-локально-зелено-в-CI-красно-три-разных-окружения.md

[Открыть копию](project/.patches/005-%D0%BB%D0%BE%D0%BA%D0%B0%D0%BB%D1%8C%D0%BD%D0%BE-%D0%B7%D0%B5%D0%BB%D0%B5%D0%BD%D0%BE-%D0%B2-CI-%D0%BA%D1%80%D0%B0%D1%81%D0%BD%D0%BE-%D1%82%D1%80%D0%B8-%D1%80%D0%B0%D0%B7%D0%BD%D1%8B%D1%85-%D0%BE%D0%BA%D1%80%D1%83%D0%B6%D0%B5%D0%BD%D0%B8%D1%8F.md)

Источник: `/home/astra/Документы/astra-voice/.patches/005-локально-зелено-в-CI-красно-три-разных-окружения.md`

SHA256 копии: `db1bfe472e08736feb894fe549d670d149cbeec02554b838c3e9660b6170fff8`

### project/.patches/006-пустой-вывод-при-нормальном-времени-это-ядро-CPU-а-не-декодер.md

[Открыть копию](project/.patches/006-%D0%BF%D1%83%D1%81%D1%82%D0%BE%D0%B9-%D0%B2%D1%8B%D0%B2%D0%BE%D0%B4-%D0%BF%D1%80%D0%B8-%D0%BD%D0%BE%D1%80%D0%BC%D0%B0%D0%BB%D1%8C%D0%BD%D0%BE%D0%BC-%D0%B2%D1%80%D0%B5%D0%BC%D0%B5%D0%BD%D0%B8-%D1%8D%D1%82%D0%BE-%D1%8F%D0%B4%D1%80%D0%BE-CPU-%D0%B0-%D0%BD%D0%B5-%D0%B4%D0%B5%D0%BA%D0%BE%D0%B4%D0%B5%D1%80.md)

Источник: `/home/astra/Документы/astra-voice/.patches/006-пустой-вывод-при-нормальном-времени-это-ядро-CPU-а-не-декодер.md`

SHA256 копии: `e22b10c800c6922fcf202cdd30214091cb039716eae7603b782f83bd4802fb39`

### project/.patches/007-тест-зелёный-на-невозможных-данных-и-окно-до-первого-события.md

[Открыть копию](project/.patches/007-%D1%82%D0%B5%D1%81%D1%82-%D0%B7%D0%B5%D0%BB%D1%91%D0%BD%D1%8B%D0%B9-%D0%BD%D0%B0-%D0%BD%D0%B5%D0%B2%D0%BE%D0%B7%D0%BC%D0%BE%D0%B6%D0%BD%D1%8B%D1%85-%D0%B4%D0%B0%D0%BD%D0%BD%D1%8B%D1%85-%D0%B8-%D0%BE%D0%BA%D0%BD%D0%BE-%D0%B4%D0%BE-%D0%BF%D0%B5%D1%80%D0%B2%D0%BE%D0%B3%D0%BE-%D1%81%D0%BE%D0%B1%D1%8B%D1%82%D0%B8%D1%8F.md)

Источник: `/home/astra/Документы/astra-voice/.patches/007-тест-зелёный-на-невозможных-данных-и-окно-до-первого-события.md`

SHA256 копии: `49b0f8eee69bb676f801b594e4525d3dc0d0bc1fdaba4e6fd4461288631dd71e`

### project/.patches/008-установленный-пакет-это-четвёртое-окружение-лаунчер-рядом-а-не-внутри.md

[Открыть копию](project/.patches/008-%D1%83%D1%81%D1%82%D0%B0%D0%BD%D0%BE%D0%B2%D0%BB%D0%B5%D0%BD%D0%BD%D1%8B%D0%B9-%D0%BF%D0%B0%D0%BA%D0%B5%D1%82-%D1%8D%D1%82%D0%BE-%D1%87%D0%B5%D1%82%D0%B2%D1%91%D1%80%D1%82%D0%BE%D0%B5-%D0%BE%D0%BA%D1%80%D1%83%D0%B6%D0%B5%D0%BD%D0%B8%D0%B5-%D0%BB%D0%B0%D1%83%D0%BD%D1%87%D0%B5%D1%80-%D1%80%D1%8F%D0%B4%D0%BE%D0%BC-%D0%B0-%D0%BD%D0%B5-%D0%B2%D0%BD%D1%83%D1%82%D1%80%D0%B8.md)

Источник: `/home/astra/Документы/astra-voice/.patches/008-установленный-пакет-это-четвёртое-окружение-лаунчер-рядом-а-не-внутри.md`

SHA256 копии: `20fe425b5f6dc99b11f45658c31c30e58afcc48cf0060c5a0fa79e84a9dde7a7`

### project/.patches/009-цикл-select-не-будится-событиями-других-потоков-ступень-250-мс.md

[Открыть копию](project/.patches/009-%D1%86%D0%B8%D0%BA%D0%BB-select-%D0%BD%D0%B5-%D0%B1%D1%83%D0%B4%D0%B8%D1%82%D1%81%D1%8F-%D1%81%D0%BE%D0%B1%D1%8B%D1%82%D0%B8%D1%8F%D0%BC%D0%B8-%D0%B4%D1%80%D1%83%D0%B3%D0%B8%D1%85-%D0%BF%D0%BE%D1%82%D0%BE%D0%BA%D0%BE%D0%B2-%D1%81%D1%82%D1%83%D0%BF%D0%B5%D0%BD%D1%8C-250-%D0%BC%D1%81.md)

Источник: `/home/astra/Документы/astra-voice/.patches/009-цикл-select-не-будится-событиями-других-потоков-ступень-250-мс.md`

SHA256 копии: `79a652d1db1a2be576f5f6c3a0ee6f83ee055aaeb12c76553c5036e9ab13cf1d`

### project/.patches/010-бриф-микрошага-без-mypy-и-чистого-окружения-пропускает-дыру.md

[Открыть копию](project/.patches/010-%D0%B1%D1%80%D0%B8%D1%84-%D0%BC%D0%B8%D0%BA%D1%80%D0%BE%D1%88%D0%B0%D0%B3%D0%B0-%D0%B1%D0%B5%D0%B7-mypy-%D0%B8-%D1%87%D0%B8%D1%81%D1%82%D0%BE%D0%B3%D0%BE-%D0%BE%D0%BA%D1%80%D1%83%D0%B6%D0%B5%D0%BD%D0%B8%D1%8F-%D0%BF%D1%80%D0%BE%D0%BF%D1%83%D1%81%D0%BA%D0%B0%D0%B5%D1%82-%D0%B4%D1%8B%D1%80%D1%83.md)

Источник: `/home/astra/Документы/astra-voice/.patches/010-бриф-микрошага-без-mypy-и-чистого-окружения-пропускает-дыру.md`

SHA256 копии: `dcf7bfed6adb0d03e83ad0d842180d44aaf888e60129b1addd27082192274890`

### project/.patches/011-зелёный-xvfb-на-подставном-мосте-контракт-QML-Python-не-проверялся.md

[Открыть копию](project/.patches/011-%D0%B7%D0%B5%D0%BB%D1%91%D0%BD%D1%8B%D0%B9-xvfb-%D0%BD%D0%B0-%D0%BF%D0%BE%D0%B4%D1%81%D1%82%D0%B0%D0%B2%D0%BD%D0%BE%D0%BC-%D0%BC%D0%BE%D1%81%D1%82%D0%B5-%D0%BA%D0%BE%D0%BD%D1%82%D1%80%D0%B0%D0%BA%D1%82-QML-Python-%D0%BD%D0%B5-%D0%BF%D1%80%D0%BE%D0%B2%D0%B5%D1%80%D1%8F%D0%BB%D1%81%D1%8F.md)

Источник: `/home/astra/Документы/astra-voice/.patches/011-зелёный-xvfb-на-подставном-мосте-контракт-QML-Python-не-проверялся.md`

SHA256 копии: `014f78af4c55c5e3eafc6f4f626de261789d62133b253a57f2ad7f2ee54a0ecf`

### project/.patches/012-git-add-A-при-работающих-зонах-утащил-чужие-файлы-и-временные-снимки.md

[Открыть копию](project/.patches/012-git-add-A-%D0%BF%D1%80%D0%B8-%D1%80%D0%B0%D0%B1%D0%BE%D1%82%D0%B0%D1%8E%D1%89%D0%B8%D1%85-%D0%B7%D0%BE%D0%BD%D0%B0%D1%85-%D1%83%D1%82%D0%B0%D1%89%D0%B8%D0%BB-%D1%87%D1%83%D0%B6%D0%B8%D0%B5-%D1%84%D0%B0%D0%B9%D0%BB%D1%8B-%D0%B8-%D0%B2%D1%80%D0%B5%D0%BC%D0%B5%D0%BD%D0%BD%D1%8B%D0%B5-%D1%81%D0%BD%D0%B8%D0%BC%D0%BA%D0%B8.md)

Источник: `/home/astra/Документы/astra-voice/.patches/012-git-add-A-при-работающих-зонах-утащил-чужие-файлы-и-временные-снимки.md`

SHA256 копии: `71a4bf741279475a137b4013f7d5600076afe41ed22737985fc659a18fdfb6a7`

### project/.patches/013-переснимать-только-затронутые-экраны-18-из-48-референсов-протухли.md

[Открыть копию](project/.patches/013-%D0%BF%D0%B5%D1%80%D0%B5%D1%81%D0%BD%D0%B8%D0%BC%D0%B0%D1%82%D1%8C-%D1%82%D0%BE%D0%BB%D1%8C%D0%BA%D0%BE-%D0%B7%D0%B0%D1%82%D1%80%D0%BE%D0%BD%D1%83%D1%82%D1%8B%D0%B5-%D1%8D%D0%BA%D1%80%D0%B0%D0%BD%D1%8B-18-%D0%B8%D0%B7-48-%D1%80%D0%B5%D1%84%D0%B5%D1%80%D0%B5%D0%BD%D1%81%D0%BE%D0%B2-%D0%BF%D1%80%D0%BE%D1%82%D1%83%D1%85%D0%BB%D0%B8.md)

Источник: `/home/astra/Документы/astra-voice/.patches/013-переснимать-только-затронутые-экраны-18-из-48-референсов-протухли.md`

SHA256 копии: `c4e16003bf817370cbf9f0d23919d48c41f2063a41bf89abf46c5b64288448f7`

### project/.patches/014-агент-прогнал-xvfb-на-живом-дисплее-заказчика-бриф-без-offscreen.md

[Открыть копию](project/.patches/014-%D0%B0%D0%B3%D0%B5%D0%BD%D1%82-%D0%BF%D1%80%D0%BE%D0%B3%D0%BD%D0%B0%D0%BB-xvfb-%D0%BD%D0%B0-%D0%B6%D0%B8%D0%B2%D0%BE%D0%BC-%D0%B4%D0%B8%D1%81%D0%BF%D0%BB%D0%B5%D0%B5-%D0%B7%D0%B0%D0%BA%D0%B0%D0%B7%D1%87%D0%B8%D0%BA%D0%B0-%D0%B1%D1%80%D0%B8%D1%84-%D0%B1%D0%B5%D0%B7-offscreen.md)

Источник: `/home/astra/Документы/astra-voice/.patches/014-агент-прогнал-xvfb-на-живом-дисплее-заказчика-бриф-без-offscreen.md`

SHA256 копии: `942fac4e8ff2e304e3d3e7e1d0cfbf6c2a265fb079dea0f5070120de53032d3c`

### project/.patches/015-test-harness-must-not-depend-on-patched-apis.md

[Открыть копию](project/.patches/015-test-harness-must-not-depend-on-patched-apis.md)

Источник: `/home/astra/Документы/astra-voice/.patches/015-test-harness-must-not-depend-on-patched-apis.md`

SHA256 копии: `24d05eddaba71eb9ee2b48797080cd6dca685c11618194ca4b1ce40d63c020ff`

### project/.patches/016-tests-xdg-isolation-and-shared-worktree.md

[Открыть копию](project/.patches/016-tests-xdg-isolation-and-shared-worktree.md)

Источник: `/home/astra/Документы/astra-voice/.patches/016-tests-xdg-isolation-and-shared-worktree.md`

SHA256 копии: `34b5a287c5e79b3f41d3c2ac0c561c149813313faae785cd275027c00da7af83`

### project/.patches/017-fly-ветки-не-проверены-по-kde-экран-без-данных-моста.md

[Открыть копию](project/.patches/017-fly-%D0%B2%D0%B5%D1%82%D0%BA%D0%B8-%D0%BD%D0%B5-%D0%BF%D1%80%D0%BE%D0%B2%D0%B5%D1%80%D0%B5%D0%BD%D1%8B-%D0%BF%D0%BE-kde-%D1%8D%D0%BA%D1%80%D0%B0%D0%BD-%D0%B1%D0%B5%D0%B7-%D0%B4%D0%B0%D0%BD%D0%BD%D1%8B%D1%85-%D0%BC%D0%BE%D1%81%D1%82%D0%B0.md)

Источник: `/home/astra/Документы/astra-voice/.patches/017-fly-ветки-не-проверены-по-kde-экран-без-данных-моста.md`

SHA256 копии: `70aa6b5eb52d2af4ab418f3a49007eaf724b6681d664e9121229f220c729a019`

### project/.patches/018-codex-завис-после-сна-ноутбука-агент-ждал-вечно.md

[Открыть копию](project/.patches/018-codex-%D0%B7%D0%B0%D0%B2%D0%B8%D1%81-%D0%BF%D0%BE%D1%81%D0%BB%D0%B5-%D1%81%D0%BD%D0%B0-%D0%BD%D0%BE%D1%83%D1%82%D0%B1%D1%83%D0%BA%D0%B0-%D0%B0%D0%B3%D0%B5%D0%BD%D1%82-%D0%B6%D0%B4%D0%B0%D0%BB-%D0%B2%D0%B5%D1%87%D0%BD%D0%BE.md)

Источник: `/home/astra/Документы/astra-voice/.patches/018-codex-завис-после-сна-ноутбука-агент-ждал-вечно.md`

SHA256 копии: `7d114ae8e7afa21fb46a84c9c3852797b6e34034c86cc5585750a24790f994de`

### project/.patches/019-shape-в-программном-рендере-не-обрезается-снимки-grabwindow-прячут-дефект.md

[Открыть копию](project/.patches/019-shape-%D0%B2-%D0%BF%D1%80%D0%BE%D0%B3%D1%80%D0%B0%D0%BC%D0%BC%D0%BD%D0%BE%D0%BC-%D1%80%D0%B5%D0%BD%D0%B4%D0%B5%D1%80%D0%B5-%D0%BD%D0%B5-%D0%BE%D0%B1%D1%80%D0%B5%D0%B7%D0%B0%D0%B5%D1%82%D1%81%D1%8F-%D1%81%D0%BD%D0%B8%D0%BC%D0%BA%D0%B8-grabwindow-%D0%BF%D1%80%D1%8F%D1%87%D1%83%D1%82-%D0%B4%D0%B5%D1%84%D0%B5%D0%BA%D1%82.md)

Источник: `/home/astra/Документы/astra-voice/.patches/019-shape-в-программном-рендере-не-обрезается-снимки-grabwindow-прячут-дефект.md`

SHA256 копии: `b8f0c842070375d0eb8a7e5294834a8d649285bf993ca5b8f88ce381320191d3`

### project/.patches/020-ci-красный-сутки-после-push-никто-не-смотрел.md

[Открыть копию](project/.patches/020-ci-%D0%BA%D1%80%D0%B0%D1%81%D0%BD%D1%8B%D0%B9-%D1%81%D1%83%D1%82%D0%BA%D0%B8-%D0%BF%D0%BE%D1%81%D0%BB%D0%B5-push-%D0%BD%D0%B8%D0%BA%D1%82%D0%BE-%D0%BD%D0%B5-%D1%81%D0%BC%D0%BE%D1%82%D1%80%D0%B5%D0%BB.md)

Источник: `/home/astra/Документы/astra-voice/.patches/020-ci-красный-сутки-после-push-никто-не-смотрел.md`

SHA256 копии: `2843ef27507dc6b4f8cb8b7ea5ad506b0571e0f3d6b8d9d9a1ba905cdadce722`

### project/.patches/021-лямбда-на-destroyed-чужого-объекта-segfault-через-gc-pyqt.md

[Открыть копию](project/.patches/021-%D0%BB%D1%8F%D0%BC%D0%B1%D0%B4%D0%B0-%D0%BD%D0%B0-destroyed-%D1%87%D1%83%D0%B6%D0%BE%D0%B3%D0%BE-%D0%BE%D0%B1%D1%8A%D0%B5%D0%BA%D1%82%D0%B0-segfault-%D1%87%D0%B5%D1%80%D0%B5%D0%B7-gc-pyqt.md)

Источник: `/home/astra/Документы/astra-voice/.patches/021-лямбда-на-destroyed-чужого-объекта-segfault-через-gc-pyqt.md`

SHA256 копии: `ba7d19af3d4b7674dd4bd4c3c3895529ef7bd71b400fd1b7ea7634031c473b68`

### project/.patches/022-пересоздание-qapplication-в-тестах-sip-не-помечает-удалённые-и-агент-в-шине-заказчика.md

[Открыть копию](project/.patches/022-%D0%BF%D0%B5%D1%80%D0%B5%D1%81%D0%BE%D0%B7%D0%B4%D0%B0%D0%BD%D0%B8%D0%B5-qapplication-%D0%B2-%D1%82%D0%B5%D1%81%D1%82%D0%B0%D1%85-sip-%D0%BD%D0%B5-%D0%BF%D0%BE%D0%BC%D0%B5%D1%87%D0%B0%D0%B5%D1%82-%D1%83%D0%B4%D0%B0%D0%BB%D1%91%D0%BD%D0%BD%D1%8B%D0%B5-%D0%B8-%D0%B0%D0%B3%D0%B5%D0%BD%D1%82-%D0%B2-%D1%88%D0%B8%D0%BD%D0%B5-%D0%B7%D0%B0%D0%BA%D0%B0%D0%B7%D1%87%D0%B8%D0%BA%D0%B0.md)

Источник: `/home/astra/Документы/astra-voice/.patches/022-пересоздание-qapplication-в-тестах-sip-не-помечает-удалённые-и-агент-в-шине-заказчика.md`

SHA256 копии: `8968733462f14401c951e5ff50f7a7ad38f5801a14112f8ec900336b9acba99a`

### project/.patches/023-xvfb-тесты-локально-гоняются-в-offscreen-а-в-ci-на-xcb-shortcut-ждёт-активного-окна.md

[Открыть копию](project/.patches/023-xvfb-%D1%82%D0%B5%D1%81%D1%82%D1%8B-%D0%BB%D0%BE%D0%BA%D0%B0%D0%BB%D1%8C%D0%BD%D0%BE-%D0%B3%D0%BE%D0%BD%D1%8F%D1%8E%D1%82%D1%81%D1%8F-%D0%B2-offscreen-%D0%B0-%D0%B2-ci-%D0%BD%D0%B0-xcb-shortcut-%D0%B6%D0%B4%D1%91%D1%82-%D0%B0%D0%BA%D1%82%D0%B8%D0%B2%D0%BD%D0%BE%D0%B3%D0%BE-%D0%BE%D0%BA%D0%BD%D0%B0.md)

Источник: `/home/astra/Документы/astra-voice/.patches/023-xvfb-тесты-локально-гоняются-в-offscreen-а-в-ci-на-xcb-shortcut-ждёт-активного-окна.md`

SHA256 копии: `7fe7a4cbdf390e464939b5c8bc2b0c811815ba3086865cf7558a584ffe2ad18a`

### project/.patches/024-синхронный-notify-с-бюджетом-450-мс-считал-поздний-ответ-провалом-dbus-run-session-поднимал-службы.md

[Открыть копию](project/.patches/024-%D1%81%D0%B8%D0%BD%D1%85%D1%80%D0%BE%D0%BD%D0%BD%D1%8B%D0%B9-notify-%D1%81-%D0%B1%D1%8E%D0%B4%D0%B6%D0%B5%D1%82%D0%BE%D0%BC-450-%D0%BC%D1%81-%D1%81%D1%87%D0%B8%D1%82%D0%B0%D0%BB-%D0%BF%D0%BE%D0%B7%D0%B4%D0%BD%D0%B8%D0%B9-%D0%BE%D1%82%D0%B2%D0%B5%D1%82-%D0%BF%D1%80%D0%BE%D0%B2%D0%B0%D0%BB%D0%BE%D0%BC-dbus-run-session-%D0%BF%D0%BE%D0%B4%D0%BD%D0%B8%D0%BC%D0%B0%D0%BB-%D1%81%D0%BB%D1%83%D0%B6%D0%B1%D1%8B.md)

Источник: `/home/astra/Документы/astra-voice/.patches/024-синхронный-notify-с-бюджетом-450-мс-считал-поздний-ответ-провалом-dbus-run-session-поднимал-службы.md`

SHA256 копии: `20fb85c589c1b12029142d28512661f66037f29e0b9dab79ce7c393bec54c747`

### project/.patches/025-qobject-из-python-удалялся-в-рабочем-qthread-взаимная-блокировка-gil-и-мьютекса-сигналов-qt.md

[Открыть копию](project/.patches/025-qobject-%D0%B8%D0%B7-python-%D1%83%D0%B4%D0%B0%D0%BB%D1%8F%D0%BB%D1%81%D1%8F-%D0%B2-%D1%80%D0%B0%D0%B1%D0%BE%D1%87%D0%B5%D0%BC-qthread-%D0%B2%D0%B7%D0%B0%D0%B8%D0%BC%D0%BD%D0%B0%D1%8F-%D0%B1%D0%BB%D0%BE%D0%BA%D0%B8%D1%80%D0%BE%D0%B2%D0%BA%D0%B0-gil-%D0%B8-%D0%BC%D1%8C%D1%8E%D1%82%D0%B5%D0%BA%D1%81%D0%B0-%D1%81%D0%B8%D0%B3%D0%BD%D0%B0%D0%BB%D0%BE%D0%B2-qt.md)

Источник: `/home/astra/Документы/astra-voice/.patches/025-qobject-из-python-удалялся-в-рабочем-qthread-взаимная-блокировка-gil-и-мьютекса-сигналов-qt.md`

SHA256 копии: `3d7946cc0815bee99a9c9da364237456a71609d5e8af7069eba79f8211867a44`

### project/.patches/026-фикс-025-через-movetothread-и-sip-delete-в-трее-повесил-остановку-qtdbus-держит-blockingqueued-на-destroyed.md

[Открыть копию](project/.patches/026-%D1%84%D0%B8%D0%BA%D1%81-025-%D1%87%D0%B5%D1%80%D0%B5%D0%B7-movetothread-%D0%B8-sip-delete-%D0%B2-%D1%82%D1%80%D0%B5%D0%B5-%D0%BF%D0%BE%D0%B2%D0%B5%D1%81%D0%B8%D0%BB-%D0%BE%D1%81%D1%82%D0%B0%D0%BD%D0%BE%D0%B2%D0%BA%D1%83-qtdbus-%D0%B4%D0%B5%D1%80%D0%B6%D0%B8%D1%82-blockingqueued-%D0%BD%D0%B0-destroyed.md)

Источник: `/home/astra/Документы/astra-voice/.patches/026-фикс-025-через-movetothread-и-sip-delete-в-трее-повесил-остановку-qtdbus-держит-blockingqueued-на-destroyed.md`

SHA256 копии: `0e688de16375af4697f9a8663e7fa202db5c7d06a828b8afefece29cc21ff85c`

### project/.patches/027-проба-захвата-на-окне-фокуса-давала-focusout-ungrab-kwin-снимал-активацию-enter-в-форме-cowork.md

[Открыть копию](project/.patches/027-%D0%BF%D1%80%D0%BE%D0%B1%D0%B0-%D0%B7%D0%B0%D1%85%D0%B2%D0%B0%D1%82%D0%B0-%D0%BD%D0%B0-%D0%BE%D0%BA%D0%BD%D0%B5-%D1%84%D0%BE%D0%BA%D1%83%D1%81%D0%B0-%D0%B4%D0%B0%D0%B2%D0%B0%D0%BB%D0%B0-focusout-ungrab-kwin-%D1%81%D0%BD%D0%B8%D0%BC%D0%B0%D0%BB-%D0%B0%D0%BA%D1%82%D0%B8%D0%B2%D0%B0%D1%86%D0%B8%D1%8E-enter-%D0%B2-%D1%84%D0%BE%D1%80%D0%BC%D0%B5-cowork.md)

Источник: `/home/astra/Документы/astra-voice/.patches/027-проба-захвата-на-окне-фокуса-давала-focusout-ungrab-kwin-снимал-активацию-enter-в-форме-cowork.md`

SHA256 копии: `00bb47e78602d0b08d08d72fe330e40f867b3f79a927fc199f1d2a3cb82fe2c6`

### project/.patches/028-перезахват-хоткея-по-mappingnotify-через-ungrab-grab-молча-терял-клавишу-диктовка-не-стартовала.md

[Открыть копию](project/.patches/028-%D0%BF%D0%B5%D1%80%D0%B5%D0%B7%D0%B0%D1%85%D0%B2%D0%B0%D1%82-%D1%85%D0%BE%D1%82%D0%BA%D0%B5%D1%8F-%D0%BF%D0%BE-mappingnotify-%D1%87%D0%B5%D1%80%D0%B5%D0%B7-ungrab-grab-%D0%BC%D0%BE%D0%BB%D1%87%D0%B0-%D1%82%D0%B5%D1%80%D1%8F%D0%BB-%D0%BA%D0%BB%D0%B0%D0%B2%D0%B8%D1%88%D1%83-%D0%B4%D0%B8%D0%BA%D1%82%D0%BE%D0%B2%D0%BA%D0%B0-%D0%BD%D0%B5-%D1%81%D1%82%D0%B0%D1%80%D1%82%D0%BE%D0%B2%D0%B0%D0%BB%D0%B0.md)

Источник: `/home/astra/Документы/astra-voice/.patches/028-перезахват-хоткея-по-mappingnotify-через-ungrab-grab-молча-терял-клавишу-диктовка-не-стартовала.md`

SHA256 копии: `b11691ee1c712539ce863f37531beb3b679d2a36f9eb9f72f0177c5143192681`

### project/.patches/029-squash-слияние-стопки-веток-ломает-следующую-ветку-x-ours-портит-хунки.md

[Открыть копию](project/.patches/029-squash-%D1%81%D0%BB%D0%B8%D1%8F%D0%BD%D0%B8%D0%B5-%D1%81%D1%82%D0%BE%D0%BF%D0%BA%D0%B8-%D0%B2%D0%B5%D1%82%D0%BE%D0%BA-%D0%BB%D0%BE%D0%BC%D0%B0%D0%B5%D1%82-%D1%81%D0%BB%D0%B5%D0%B4%D1%83%D1%8E%D1%89%D1%83%D1%8E-%D0%B2%D0%B5%D1%82%D0%BA%D1%83-x-ours-%D0%BF%D0%BE%D1%80%D1%82%D0%B8%D1%82-%D1%85%D1%83%D0%BD%D0%BA%D0%B8.md)

Источник: `/home/astra/Документы/astra-voice/.patches/029-squash-слияние-стопки-веток-ломает-следующую-ветку-x-ours-портит-хунки.md`

SHA256 копии: `d0df289125a72a5e5716b0f4d4e1fc1f5695c7fb12052bafbc719978f9feef08`

### project/.patches/INDEX.md

[Открыть копию](project/.patches/INDEX.md)

Источник: `/home/astra/Документы/astra-voice/.patches/INDEX.md`

SHA256 копии: `d20fdfdfe590c78a8a4be3c4c92b1f3a98f557a16c004299307fa831575a0ead`

### project/AGENTS.md

[Открыть копию](project/AGENTS.md)

Источник: `/home/astra/Документы/astra-voice/AGENTS.md`

SHA256 копии: `dbd0527855ebbcfaf036444668acb79181352965a75eab164401d50b94bcff41`

### project/HEARTBEAT.md

[Открыть копию](project/HEARTBEAT.md)

Источник: `/home/astra/Документы/astra-voice/HEARTBEAT.md`

SHA256 копии: `96d9e9b6d43662596816ffdabd3c93ba3022819078417c401d461d63dd1596e4`

### project/LICENSE.txt

[Открыть копию](project/LICENSE.txt)

Источник: `/home/astra/Документы/astra-voice/LICENSE`

SHA256 копии: `3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986`

### project/NOTICE.txt

[Открыть копию](project/NOTICE.txt)

Источник: `/home/astra/Документы/astra-voice/NOTICE`

SHA256 копии: `e135129c70fd8f300669ede627e9fd9affe07d5eb418d8e80eeb45852d4ebb8d`

### project/PRD.md

[Открыть копию](project/PRD.md)

Источник: `/home/astra/Документы/astra-voice/PRD.md`

SHA256 копии: `f0304f219a8dd0bc5174f0820fc193aa8a57eeccff1bb5a3a8c5cd5c2f3bbea7`

### project/PROJECT.md

[Открыть копию](project/PROJECT.md)

Источник: `/home/astra/Документы/astra-voice/PROJECT.md`

SHA256 копии: `3043bcdeed03bc144e4dfe6eda4a95d344c912c6ea97f5caedce7e6c17f82a00`

### project/README.md

[Открыть копию](project/README.md)

Источник: `/home/astra/Документы/astra-voice/README.md`

SHA256 копии: `f1212fb50d83471d6d63afbc0e6e8d3e1e48dc4e0645d272ea37e92b00a23aed`

### project/arch/appimage.md

[Открыть копию](project/arch/appimage.md)

Источник: `/home/astra/Документы/astra-voice/arch/appimage.md`

SHA256 копии: `03bdf467558bdb9284e791131e0de898b5cc64c2882fbe82b22e9c0ca6093bc3`

### project/arch/challenge-astra-cowork-contract.md

[Открыть копию](project/arch/challenge-astra-cowork-contract.md)

Источник: `/home/astra/Документы/astra-voice/arch/challenge-astra-cowork-contract.md`

SHA256 копии: `e2573e97c61ef44fa5cc8209ef25ea50251cbe8f61a6fdb3c83d9736541a81e0`

### project/arch/challenge-astra.md

[Открыть копию](project/arch/challenge-astra.md)

Источник: `/home/astra/Документы/astra-voice/arch/challenge-astra.md`

SHA256 копии: `ba8ffa4d42db266fd7791a08f0937c466ca24bb2728fbae8b8e1a93cc08c5de2`

### project/arch/onboarding-repeat.md

[Открыть копию](project/arch/onboarding-repeat.md)

Источник: `/home/astra/Документы/astra-voice/arch/onboarding-repeat.md`

SHA256 копии: `49f796400701e1bc5414d783267fed80982a23de2ccffe3ebd0eb51a11bad2a0`

### project/arch/plan-claude-cowork-contract.md

[Открыть копию](project/arch/plan-claude-cowork-contract.md)

Источник: `/home/astra/Документы/astra-voice/arch/plan-claude-cowork-contract.md`

SHA256 копии: `ee7ab18ba33c16d7c239b39bf67939e722d9c0da27266dd94de83dcf929b2de2`

### project/arch/plan-claude.md

[Открыть копию](project/arch/plan-claude.md)

Источник: `/home/astra/Документы/astra-voice/arch/plan-claude.md`

SHA256 копии: `73c27301edcaeed3c55c72f893d1ec171d44411f98ea059dc468f8490bb0d72a`

### project/arch/plan-codex-cowork-contract.md

[Открыть копию](project/arch/plan-codex-cowork-contract.md)

Источник: `/home/astra/Документы/astra-voice/arch/plan-codex-cowork-contract.md`

SHA256 копии: `756e8413b7ccb2d2934028854aa8545716f6850d324ecf8423b7c79d5d05ffe4`

### project/arch/plan-codex.md

[Открыть копию](project/arch/plan-codex.md)

Источник: `/home/astra/Документы/astra-voice/arch/plan-codex.md`

SHA256 копии: `7a10d08944e77dacfb7eb141468d64931a8e2341c63e8e9c556f2a94d79a65be`

### project/arch/plan-synth-cowork-contract.md

[Открыть копию](project/arch/plan-synth-cowork-contract.md)

Источник: `/home/astra/Документы/astra-voice/arch/plan-synth-cowork-contract.md`

SHA256 копии: `db392232b639e756853009afc79c4548c66501928d1d55501b958fe23054b59f`

### project/arch/plan-synth.md

[Открыть копию](project/arch/plan-synth.md)

Источник: `/home/astra/Документы/astra-voice/arch/plan-synth.md`

SHA256 копии: `2539f6fe635e7f756565065f33d5d6dff164d0b21a19b327ae57bee2fa18ea13`

### project/arch/s5a5-mic-change.md

[Открыть копию](project/arch/s5a5-mic-change.md)

Источник: `/home/astra/Документы/astra-voice/arch/s5a5-mic-change.md`

SHA256 копии: `98f82b7201b898d0f65c6770f2859c510439cba6e484455651d6af5ee67b673a`

### project/arch/spikes/S1.md

[Открыть копию](project/arch/spikes/S1.md)

Источник: `/home/astra/Документы/astra-voice/arch/spikes/S1.md`

SHA256 копии: `ab405ecc30a3f570cc17d65a3a843b39252ad00543022e047d4f8203f37c8242`

### project/arch/spikes/S2.md

[Открыть копию](project/arch/spikes/S2.md)

Источник: `/home/astra/Документы/astra-voice/arch/spikes/S2.md`

SHA256 копии: `a2d6ba9234c4e1623a0ea0daaff8a3fc609db3d2c5fd017cf65a11648f48a46f`

### project/arch/spikes/S3.md

[Открыть копию](project/arch/spikes/S3.md)

Источник: `/home/astra/Документы/astra-voice/arch/spikes/S3.md`

SHA256 копии: `5c2de8a3f804ea43ee366778a0fafd93ba9abcffbefab15a3011f682ed907820`

### project/arch/spikes/S4.md

[Открыть копию](project/arch/spikes/S4.md)

Источник: `/home/astra/Документы/astra-voice/arch/spikes/S4.md`

SHA256 копии: `b8b90f234c6e15535dd639f20273c83c36567f13f926594aeb476565b1a78deb`

### project/arch/spikes/S5.md

[Открыть копию](project/arch/spikes/S5.md)

Источник: `/home/astra/Документы/astra-voice/arch/spikes/S5.md`

SHA256 копии: `6fe392d08e7b0aef76e780dc270c3d6ebf6e5682b9ae6ffd35e62e42db66bdea`

### project/arch/spikes/S5A5.md

[Открыть копию](project/arch/spikes/S5A5.md)

Источник: `/home/astra/Документы/astra-voice/arch/spikes/S5A5.md`

SHA256 копии: `3cf6ed3785b3df41a8c82464c9e97c231f111519be71ce017216ac2a147a8463`

### project/arch/spikes/S9.md

[Открыть копию](project/arch/spikes/S9.md)

Источник: `/home/astra/Документы/astra-voice/arch/spikes/S9.md`

SHA256 копии: `6979e72e2b9b3e643757e85142ad7f7c5aab85ebb3b8d55a15bba9692145064c`

### project/backlog.md

[Открыть копию](project/backlog.md)

Источник: `/home/astra/Документы/astra-voice/backlog.md`

SHA256 копии: `3408ba21f48d3cf62f2d1b4913dea57e7488799a83c782c5a68f2c517a6f51ff`

### project/data/keys/README.md

[Открыть копию](project/data/keys/README.md)

Источник: `/home/astra/Документы/astra-voice/data/keys/README.md`

SHA256 копии: `2e51c2f85cf24d9a5c99bef52e72393b046555f7eca74a3d67f83197da694f3a`

### project/data/smoke/README.md

[Открыть копию](project/data/smoke/README.md)

Источник: `/home/astra/Документы/astra-voice/data/smoke/README.md`

SHA256 копии: `98bd2115c5bcb1f2df79a1805e229209ecef6b1ef0e345298e66b7bad136c624`

### project/data/test/README.md

[Открыть копию](project/data/test/README.md)

Источник: `/home/astra/Документы/astra-voice/data/test/README.md`

SHA256 копии: `aeb067b9f40d925cd2c0eaef5134c7db1636a5aed2ea2c8a17edc20bfeee7e20`

### project/data/vad/README.md

[Открыть копию](project/data/vad/README.md)

Источник: `/home/astra/Документы/astra-voice/data/vad/README.md`

SHA256 копии: `100a8cb41f00ae19ef95c9ceb6103bf3b024fd67695bdc9db7a6bb03ec877652`

### project/decisions/log.md

[Открыть копию](project/decisions/log.md)

Источник: `/home/astra/Документы/astra-voice/decisions/log.md`

SHA256 копии: `26a5a7be6e2ca0d4b3173acafd4d16471881d9bdd8baafd41055e41f9f5246e5`

### project/design/brand/brand-basics.md

[Открыть копию](project/design/brand/brand-basics.md)

Источник: `/home/astra/Документы/astra-voice/design/brand/brand-basics.md`

SHA256 копии: `93206ef226719dbaeb8cb71782cf6772fd363e2abe8b2c8a3b3c663f4fe7cc19`

### project/design/flows.md

[Открыть копию](project/design/flows.md)

Источник: `/home/astra/Документы/astra-voice/design/flows.md`

SHA256 копии: `6910151d59e1c94a5ecef99c41db7e25588afae5171929c17a8aeecda113e115`

### project/design/refs/README.md

[Открыть копию](project/design/refs/README.md)

Источник: `/home/astra/Документы/astra-voice/design/refs/README.md`

SHA256 копии: `87810fa4cea4e3700544e3aa05e66e90a8ce3368d1aa7b51ffd37334268b2869`

### project/design/spec.md

[Открыть копию](project/design/spec.md)

Источник: `/home/astra/Документы/astra-voice/design/spec.md`

SHA256 копии: `3f37795d487e56b45e5287b1a63e8274a2def4f16ed1fa1aa90a38a026c2f0f1`

### project/design/tokens.md

[Открыть копию](project/design/tokens.md)

Источник: `/home/astra/Документы/astra-voice/design/tokens.md`

SHA256 копии: `bd12f7cd60db9a00fc07768d965e8473ea55b607f5186c2b0e0a417b0bce4a49`

### project/docs/INSTALL-ADMIN.md

[Открыть копию](project/docs/INSTALL-ADMIN.md)

Источник: `/home/astra/Документы/astra-voice/docs/INSTALL-ADMIN.md`

SHA256 копии: `a5e020c10e24bf29bd8720aeea7ae66f4ee0c9e5e2c40d1e9ae8bdcc72b45734`

### project/docs/PRIVACY.md

[Открыть копию](project/docs/PRIVACY.md)

Источник: `/home/astra/Документы/astra-voice/docs/PRIVACY.md`

SHA256 копии: `9041be8b6ea755ed175fefefb041b56e23c24bb2e5ece732379a415b8463cff5`

### project/docs/SECURITY.md

[Открыть копию](project/docs/SECURITY.md)

Источник: `/home/astra/Документы/astra-voice/docs/SECURITY.md`

SHA256 копии: `b58e9056eadf539619a60a0245e55dcc76ecd9ac88995c94a5a4b325ecd63957`

### project/docs/codex-team.md

[Открыть копию](project/docs/codex-team.md)

Источник: `/home/astra/Документы/astra-voice/docs/codex-team.md`

SHA256 копии: `c2649b4522efcfee80dd7e71c7621ca726932330cb1c3297e27fdbab4def676d`

### project/docs/contracts/astra-cowork-command1.md

[Открыть копию](project/docs/contracts/astra-cowork-command1.md)

Источник: `/home/astra/Документы/astra-voice/docs/contracts/astra-cowork-command1.md`

SHA256 копии: `dd141dfcd21274b7437a88e4c55a7856eac21d0887566831c2397dbef0bcd698`

### project/docs/contracts/latest.json.md

[Открыть копию](project/docs/contracts/latest.json.md)

Источник: `/home/astra/Документы/astra-voice/docs/contracts/latest.json.md`

SHA256 копии: `61df608a9f75653920198837b22e3b5c6aba94adc7ae019d4a6e3e4781659b22`

### project/docs/plans.md

[Открыть копию](project/docs/plans.md)

Источник: `/home/astra/Документы/astra-voice/docs/plans.md`

SHA256 копии: `7fa5c66a8c1e363f4802036537a97340819a853d667c1db3849dc4a473aadcdb`

### project/docs/security/2026-09-30-t1t2-appimage.md

[Открыть копию](project/docs/security/2026-09-30-t1t2-appimage.md)

Источник: `/home/astra/Документы/astra-voice/docs/security/2026-09-30-t1t2-appimage.md`

SHA256 копии: `c504df1046c93a03722e2309019f1cdc57f3f32237201f138f60c3a54e09a319`

### project/docs/security/T1-appimage-2026-09-28.md

[Открыть копию](project/docs/security/T1-appimage-2026-09-28.md)

Источник: `/home/astra/Документы/astra-voice/docs/security/T1-appimage-2026-09-28.md`

SHA256 копии: `292af8bc68adf304591d1abd3b4e9e9788c2f48c4384bfe9e5b1bf438a254005`

### project/docs/security/T2-2026-09-26-downloader.md

[Открыть копию](project/docs/security/T2-2026-09-26-downloader.md)

Источник: `/home/astra/Документы/astra-voice/docs/security/T2-2026-09-26-downloader.md`

SHA256 копии: `ea1c4523fb5a394c91299018adda1d1e5b0a58b58cd8220eb99d90d9efafce21`

### project/docs/security/T2-2026-09-26-master-key.md

[Открыть копию](project/docs/security/T2-2026-09-26-master-key.md)

Источник: `/home/astra/Документы/astra-voice/docs/security/T2-2026-09-26-master-key.md`

SHA256 копии: `939538ea38b6bd5e8d472c18fa4123c374b40c8c4043ac4213ead89bda1d5ade`

### project/docs/security/T2-2026-09-26.md

[Открыть копию](project/docs/security/T2-2026-09-26.md)

Источник: `/home/astra/Документы/astra-voice/docs/security/T2-2026-09-26.md`

SHA256 копии: `5f1d893d97c1ce0ab3afbc6402be2c09a384b6d9b168f228c912457a3b3e39ed`

### project/docs/security/T3-2026-09-28-v0.1.md

[Открыть копию](project/docs/security/T3-2026-09-28-v0.1.md)

Источник: `/home/astra/Документы/astra-voice/docs/security/T3-2026-09-28-v0.1.md`

SHA256 копии: `c83ee5f470e1b5f9f97788d1966b4dd6c499e7551037fef5d70bf3b2334dfe1a`

### project/docs/status.md

[Открыть копию](project/docs/status.md)

Источник: `/home/astra/Документы/astra-voice/docs/status.md`

SHA256 копии: `a6ccd17af50cf951bd35c1225ad667a467715cc700fcbdec23eda4d733ea8c13`

### project/docs/test-plan.md

[Открыть копию](project/docs/test-plan.md)

Источник: `/home/astra/Документы/astra-voice/docs/test-plan.md`

SHA256 копии: `aef33f64c24dc237e0803ba10e826a095b276ec5f12b2a96558ebcd3af6bb8f7`

### project/docs/threat-model.md

[Открыть копию](project/docs/threat-model.md)

Источник: `/home/astra/Документы/astra-voice/docs/threat-model.md`

SHA256 копии: `3130fad90eb4f35278c320d354a65ef1e6ba2f9d55fcf4c370f7d13eea3436dd`

### project/docs/ui-bridge.md

[Открыть копию](project/docs/ui-bridge.md)

Источник: `/home/astra/Документы/astra-voice/docs/ui-bridge.md`

SHA256 копии: `d0efed46155eb33033ba1a0303d8541605808f9c6e0c36541c057a3dec96c7f5`

### project/packaging/appimage/ci-workflow-patch.md

[Открыть копию](project/packaging/appimage/ci-workflow-patch.md)

Источник: `/home/astra/Документы/astra-voice/packaging/appimage/ci-workflow-patch.md`

SHA256 копии: `5252cf86d55b7af2421670d7ecf942f67c3ec0d70a9d1a3092e289ce00db8b7f`

### project/research/catalog-numbers.md

[Открыть копию](project/research/catalog-numbers.md)

Источник: `/home/astra/Документы/astra-voice/research/catalog-numbers.md`

SHA256 копии: `a16bc7b2ad519da04a2c5098574d10b8702f8e951add1becc5c3ff0874767c76`

### project/research/competitors-ux.md

[Открыть копию](project/research/competitors-ux.md)

Источник: `/home/astra/Документы/astra-voice/research/competitors-ux.md`

SHA256 копии: `60ac0f41daae5dfba8884795f409b46cd03066d029986eee962f3006050ee36e`

### project/research/legal-meetings-astra.md

[Открыть копию](project/research/legal-meetings-astra.md)

Источник: `/home/astra/Документы/astra-voice/research/legal-meetings-astra.md`

SHA256 копии: `903889335b157a5ffe89c97d38bb91b5321381b2b35001b27292865ec86e20ac`

### project/research/legal-meetings.md

[Открыть копию](project/research/legal-meetings.md)

Источник: `/home/astra/Документы/astra-voice/research/legal-meetings.md`

SHA256 копии: `d8f9c8a4507b3a53ae1e5de88730b5ded6dc306ce90c353dbbb210898cf7dee9`

### project/research/legal.md

[Открыть копию](project/research/legal.md)

Источник: `/home/astra/Документы/astra-voice/research/legal.md`

SHA256 копии: `4912e250c9fd327d9c0196315cd8395e1d05acac6419f4b6ce199cc93f8a538e`

### project/research/summary.md

[Открыть копию](project/research/summary.md)

Источник: `/home/astra/Документы/astra-voice/research/summary.md`

SHA256 копии: `07ca3ae342753472dad8da5415fbe4e440a712be03d4fd209549bdabb139d7d7`

### project/research/tech-amx-int8.md

[Открыть копию](project/research/tech-amx-int8.md)

Источник: `/home/astra/Документы/astra-voice/research/tech-amx-int8.md`

SHA256 копии: `418df1c9f1842f0e5289fc152af13dab46996222e9d6d37ada35be1c2927ff33`

### project/research/tech-codex.md

[Открыть копию](project/research/tech-codex.md)

Источник: `/home/astra/Документы/astra-voice/research/tech-codex.md`

SHA256 копии: `2a3192d67b7a6705c630f89704f89a76be87a4297f768c444ad9d1df4d503f5f`

### project/research/tech-meetings.md

[Открыть копию](project/research/tech-meetings.md)

Источник: `/home/astra/Документы/astra-voice/research/tech-meetings.md`

SHA256 копии: `339ae8165a9f24f039d38dbe80d5c31204115502bfc407e079a7b634238a13e5`

### project/research/tech-models-claude.md

[Открыть копию](project/research/tech-models-claude.md)

Источник: `/home/astra/Документы/astra-voice/research/tech-models-claude.md`

SHA256 копии: `d82156194c83cfa4d5debeb670f752dc1f02e8f296a6a3ef106a9a15c9ec8962`

### project/research/tech-platform-claude.md

[Открыть копию](project/research/tech-platform-claude.md)

Источник: `/home/astra/Документы/astra-voice/research/tech-platform-claude.md`

SHA256 копии: `440c34e5ebc2c45f5a86be77435cbc94f8908d5bc5e4c423218d5b8f8249b9c1`

### project/research/vks-clients.md

[Открыть копию](project/research/vks-clients.md)

Источник: `/home/astra/Документы/astra-voice/research/vks-clients.md`

SHA256 копии: `cb1ddae33dd7a2316dbe0b1705a3001cddf630d339cff7162b2e49668f69a14e`

### project/spikes/appimage/RESEARCH-2026-09-28.md

[Открыть копию](project/spikes/appimage/RESEARCH-2026-09-28.md)

Источник: `/home/astra/Документы/astra-voice/spikes/appimage/RESEARCH-2026-09-28.md`

SHA256 копии: `cbcf070f15a1b3a3f1c518ef74452dde84f48c8dfd338211de72076d5dc821fb`

### project/spikes/m1_live/README.md

[Открыть копию](project/spikes/m1_live/README.md)

Источник: `/home/astra/Документы/astra-voice/spikes/m1_live/README.md`

SHA256 копии: `d2c2d1d5abd2fd581c2d08e02cadf0bb180c95680f951eda9970b193325ac286`

### project/spikes/m1_live/flicker.md

[Открыть копию](project/spikes/m1_live/flicker.md)

Источник: `/home/astra/Документы/astra-voice/spikes/m1_live/flicker.md`

SHA256 копии: `84115ac7959d86709d32b27c8aad0db9e390144bbc3ef452a0f88a40e79d95f4`

### project/spikes/m1_live/rss.md

[Открыть копию](project/spikes/m1_live/rss.md)

Источник: `/home/astra/Документы/astra-voice/spikes/m1_live/rss.md`

SHA256 копии: `cfaabc6f0296fb2b20b586aa5aa645dacaf0b1951be11ceb8de697482842e5b9`

### project/spikes/m4_live/README.md

[Открыть копию](project/spikes/m4_live/README.md)

Источник: `/home/astra/Документы/astra-voice/spikes/m4_live/README.md`

SHA256 копии: `09592a0ffb8994d1de8ceba724b5b3da3c88298e60f5b16da167b6990244464e`

### project/spikes/s1_pill_tray/README.md

[Открыть копию](project/spikes/s1_pill_tray/README.md)

Источник: `/home/astra/Документы/astra-voice/spikes/s1_pill_tray/README.md`

SHA256 копии: `88268dd06447b3b6a4ee6489b09b8fec360b110f8832312ea3862821fbb5119f`

### project/spikes/s2_polkit_helper/README.md

[Открыть копию](project/spikes/s2_polkit_helper/README.md)

Источник: `/home/astra/Документы/astra-voice/spikes/s2_polkit_helper/README.md`

SHA256 копии: `2f9da3e2ef8edb6db12ed242c22da011fe69a64fc712781a02adbdc381ccab08`

### project/spikes/s4_hotkey_paste/README.md

[Открыть копию](project/spikes/s4_hotkey_paste/README.md)

Источник: `/home/astra/Документы/astra-voice/spikes/s4_hotkey_paste/README.md`

SHA256 копии: `f4716ec7d3f6f543a572f19acdff4c0dcdb591e379c1178fe8174fa957686662`

### project/spikes/s5_audio/README.md

[Открыть копию](project/spikes/s5_audio/README.md)

Источник: `/home/astra/Документы/astra-voice/spikes/s5_audio/README.md`

SHA256 копии: `4c7468fd046eec45070593682941d0db4ecf9ed0a7a889b5bb971d437fa36e60`

### project/stories.md

[Открыть копию](project/stories.md)

Источник: `/home/astra/Документы/astra-voice/stories.md`

SHA256 копии: `afa93e2e576f42fbde2870c8f8619b9bac5f1ea0c2a39b9ef8f659bb77da260b`

### project/time.local.md

[Открыть копию](project/time.local.md)

Источник: `/home/astra/Документы/astra-voice/time.local.md`

SHA256 копии: `00c6b33454b7f6598ba703a4084b8a48f1d455869d1f78aaaac1e1bb2e487fa2`

## Новые навигационные документы

`00_START_HERE.md`, `01_PROJECT_STATE.md`, `INDEX.md`, `claude-memory/MEMORY.md`, `chat-history/INDEX.md` и этот реестр созданы при переносе. Это вводная, сводка и навигация по источникам.
