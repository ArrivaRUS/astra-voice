Ответы `GET https://huggingface.co/api/models/{repo}/refs` в форме API Hugging Face
(поля `branches[].name`/`targetCommit`, `converts`, `tags`). Коммиты — синтетические
(sha1 от строки-метки), сеть в тестах не используется. Тесты: `tests/unit/test_net_hf.py`,
`tests/unit/test_model_revision_updates.py` (T-200…T-209).
