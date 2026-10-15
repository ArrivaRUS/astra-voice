"""Общие идентификаторы выпусков и схемы каталога клиента."""

from __future__ import annotations

import hashlib

from astra_voice.core import paths

RELEASES_REPO = "ArrivaRUS/astra-voice"


def schema_id(schema_bytes: bytes) -> str:
    """Идентификатор точных байтов схемы для имени ассета каталога."""
    return hashlib.sha256(schema_bytes).hexdigest()[:16]


def client_schema_id() -> str:
    """Схема, поставленная с этим клиентом, независимо от удалённого каталога."""
    return schema_id((paths.data_dir_static() / "catalog.schema.json").read_bytes())


def catalog_default_url() -> str:
    return (
        f"https://github.com/{RELEASES_REPO}/releases/latest/download/"
        f"catalog-{client_schema_id()}.json"
    )
