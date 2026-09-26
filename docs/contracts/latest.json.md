# Контракт `latest.json`

`latest.json` — указатель на последний пакет релиза. Поля JSON-объекта обязательны, дополнительных полей нет.

| Поле | Тип | Значение |
|---|---|---|
| `version` | строка | Версия X.Y.Z без префикса `v` |
| `deb` | строка | Имя файла `astra-voice_X.Y.Z_<архитектура>.deb` |
| `sha256` | строка | SHA-256 пакета, 64 шестнадцатеричных символа |
| `published_at` | строка | Время публикации в UTC, ISO 8601, `YYYY-MM-DDTHH:MM:SSZ` |
| `min_astra` | строка | Минимальная версия Astra Linux SE |

```json
{
  "deb": "astra-voice_0.1.0_amd64.deb",
  "min_astra": "1.8",
  "published_at": "2026-09-30T12:00:00Z",
  "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "version": "0.1.0"
}
```

`latest.json` — неподписанный указатель. В релизе его хеш находится внутри подписанного `SHA256SUMS`. Клиент обязан проверить `SHA256SUMS.asc` доверенным ключом и сверить deb с записью в `SHA256SUMS` до установки; одного `sha256` из `latest.json` недостаточно.

Релиз содержит семь ассетов: `astra-voice_*.deb`, `sbom.cdx.json`, `SHA256SUMS`,
`SHA256SUMS.asc`, `INSTALL-ADMIN.md` (копия `docs/INSTALL-ADMIN.md`),
`latest.json` и `release.gpg` (копия `data/keys/release.gpg`). В пакете связка лежит
по пути `/usr/share/astra-voice/data/keys/release.gpg`.
Ассет `release.gpg` нельзя использовать как корень доверия для проверки того же релиза.
Отпечаток мастера нужно сверить по `README.md` и `docs/INSTALL-ADMIN.md`.
