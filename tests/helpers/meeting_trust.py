"""Explicit synthetic unit admission and real signed pair preparation for IPC tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from astra_voice.worker.meeting_transcribe import MeetingTranscribeState, _Job

ROOT = Path(__file__).resolve().parents[2]
RAW_MODEL = b"\x08\x01"


class RequestPairState(MeetingTranscribeState):
    """Unit-only admission stub; explicit providers always take precedence."""

    def __init__(self, **options: Any) -> None:
        self._admitted_request: dict[str, Any] = {}
        options.setdefault("diar_models_provider", self._admitted_pair)
        super().__init__(**options)

    def _admitted_pair(self) -> dict[str, str] | None:
        models = self._admitted_request.get("diar_models")
        return dict(models) if models else None

    def _run_job(self, job: _Job) -> None:
        self._admitted_request = job.request
        try:
            super()._run_job(job)
        finally:
            self._admitted_request = {}


class SigningUnavailable(RuntimeError):
    """A restricted runner cannot start the temporary signing agent."""


class SignedDiarPair:
    """Real Verifier, catalog loader, installed metadata and trust provider.

    By default the tiny ONNX images serve unit hash/layout checks only. Optional
    model_files install actual corpus ONNX bytes for native integrations, using
    the same signing and genuine trust provider. Defaults preserve unit behavior.
    """

    def __init__(self, root: Path, *, model_root: Path | None = None) -> None:
        self.root = root.resolve()
        self.model_root = self.root / "models" if model_root is None else model_root.resolve()

    @classmethod
    def prepare(
        cls,
        root: Path,
        *,
        model_files: Mapping[str, Path] | None = None,
        model_root: Path | None = None,
        signing_home: Path | None = None,
    ) -> SignedDiarPair:
        pair = cls.stage(root, model_files=model_files, model_root=model_root)
        pair.sign(signing_home)
        assert pair.models() is not None, "real provider rejected the signed fixture"
        return pair

    @classmethod
    def stage(
        cls,
        root: Path,
        *,
        model_files: Mapping[str, Path] | None = None,
        model_root: Path | None = None,
    ) -> SignedDiarPair:
        """Install bytes/metadata and write the catalog; no signing or trust claim yet."""
        from astra_voice.models.catalog import DIARIZATION_MODEL_IDS
        from astra_voice.models.store import ModelStore
        from astra_voice.security.files import write_private

        pair = cls(root, model_root=model_root)
        root = pair.root
        root.mkdir(mode=0o700, parents=True)
        catalog = root / "catalog"
        catalog.mkdir(mode=0o700)
        document = json.loads((ROOT / "data/catalog.json").read_bytes())
        entries = [e for e in document["models"] if e["id"] in DIARIZATION_MODEL_IDS]
        if model_root is not None:
            from astra_voice.core.paths import ensure_private_dir

            ensure_private_dir(pair.model_root)
            ensure_private_dir(pair.model_root / ".diarization")
        generation = pair.model_root / ".diarization" / ("a" * 32)
        generation.mkdir(mode=0o700, parents=True)
        store = ModelStore(generation)
        for entry in entries:
            name = entry["files"][0]["path"]
            raw = (
                RAW_MODEL
                if model_files is None
                else model_files[name].resolve(strict=True).read_bytes()
            )
            entry["size_bytes"] = len(raw)
            entry["files"][0].update(size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            directory = store.staging_dir(entry["id"], entry["revision"])
            write_private(directory / name, raw)
            write_private(
                directory / "state.json",
                json.dumps(
                    {
                        "layout": entry["layout"],
                        "variant": entry["variant"],
                        "size_bytes": len(raw),
                    }
                ).encode(),
            )
            store.commit(entry["id"], entry["revision"])
        write_private(
            generation.parent / "current.json",
            json.dumps(
                {
                    "generation": generation.name,
                    "models": [[e["id"], e["revision"]] for e in entries],
                }
            ).encode(),
        )
        write_private(catalog / "catalog.json", json.dumps(document).encode())
        write_private(
            catalog / "catalog.schema.json", (ROOT / "data/catalog.schema.json").read_bytes()
        )
        return pair

    def sign(self, signing_home: Path | None = None) -> None:
        from astra_voice.security.files import write_private

        root = self.root
        catalog = root / "catalog"
        home = root / "gnupg" if signing_home is None else signing_home
        home.mkdir(mode=0o700)

        def gpg(*args: str) -> subprocess.CompletedProcess[bytes]:
            return subprocess.run(
                [
                    "/usr/bin/gpg",
                    "--batch",
                    "--yes",
                    "--quiet",
                    "--homedir",
                    str(home),
                    "--pinentry-mode",
                    "loopback",
                    "--passphrase",
                    "",
                    *args,
                ],
                capture_output=True,
                check=True,
                timeout=8,
            )

        try:
            try:
                gpg(
                    "--quick-gen-key", "Astra fixture <fixture@invalid>", "ed25519", "sign", "never"
                )
            except subprocess.CalledProcessError as exc:
                detail = exc.stderr.decode(errors="replace")
                if "gpg-agent" in detail and (
                    "failed to start" in detail or "can't connect" in detail
                ):
                    raise SigningUnavailable(detail) from exc
                raise
            keys = gpg("--with-colons", "--list-keys").stdout.decode()
            pin = next(row.split(":")[9] for row in keys.splitlines() if row.startswith("fpr:"))
            write_private(root / "pin.txt", pin.encode())
            write_private(root / "test.gpg", gpg("--export", pin).stdout)
            gpg(
                "--detach-sign",
                "--local-user",
                pin,
                "--output",
                str(catalog / "catalog.json.sig"),
                str(catalog / "catalog.json"),
            )
        finally:
            subprocess.run(
                ["/usr/bin/gpgconf", "--homedir", str(home), "--kill", "gpg-agent"],
                capture_output=True,
                check=False,
                timeout=5,
            )

    def catalog(self) -> Any:
        from astra_voice.models.catalog import load_builtin
        from astra_voice.security.verify import Verifier

        verifier = Verifier(
            "catalog",
            keyring=self.root / "test.gpg",
            pinned=frozenset({(self.root / "pin.txt").read_text()}),
        )
        return load_builtin(verifier, root=self.root / "catalog", require_schema=True)

    def models(self) -> dict[str, str] | None:
        from astra_voice.meetings.diar_models import installed_diar_models
        from astra_voice.models.store import ModelStore

        return installed_diar_models(ModelStore(self.model_root), catalog_provider=self.catalog)
