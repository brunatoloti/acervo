"""Acesso à estante de músicas armazenada no Google Drive."""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from google.oauth2 import service_account
from google.auth.transport.requests import AuthorizedSession
from requests import Response

from acervo.library import AUDIO_EXTENSIONS, read_audio


DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
DRIVE_API_URL = "https://www.googleapis.com/drive/v3"


class DriveClient:
    """Cliente mínimo da API REST do Drive, limitado às operações de leitura."""

    def __init__(self, credentials: Any):
        self.session = AuthorizedSession(credentials)

    @staticmethod
    def _raise_for_error(response: Response) -> None:
        if response.ok:
            return
        try:
            message = response.json().get("error", {}).get("message")
        except (TypeError, ValueError):
            message = ""
        detail = message or response.reason or f"HTTP {response.status_code}"
        raise ValueError(f"Erro da API do Google Drive: {detail}")

    def metadata(self, file_id: str) -> dict[str, Any]:
        response = self.session.get(
            f"{DRIVE_API_URL}/files/{file_id}",
            params={
                "fields": "id,name,mimeType",
                "supportsAllDrives": "true",
            },
            timeout=60,
        )
        self._raise_for_error(response)
        return response.json()

    def children(self, folder_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params = {
                "q": f"'{folder_id}' in parents and trashed = false",
                "fields": (
                    "nextPageToken,files("
                    "id,name,mimeType,modifiedTime,size,parents)"
                ),
                "orderBy": "name_natural",
                "pageSize": "1000",
                "includeItemsFromAllDrives": "true",
                "supportsAllDrives": "true",
            }
            if page_token:
                params["pageToken"] = page_token
            response = self.session.get(
                f"{DRIVE_API_URL}/files", params=params, timeout=60
            )
            self._raise_for_error(response)
            payload = response.json()
            items.extend(payload.get("files", []))
            page_token = str(payload.get("nextPageToken") or "")
            if not page_token:
                return items

    def download(self, file_id: str, destination: Path) -> None:
        with self.session.get(
            f"{DRIVE_API_URL}/files/{file_id}",
            params={"alt": "media", "supportsAllDrives": "true"},
            stream=True,
            timeout=300,
        ) as response:
            self._raise_for_error(response)
            with destination.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        output.write(chunk)


def extract_folder_id(value: str) -> str:
    """Aceita tanto o ID quanto uma URL de pasta do Google Drive."""
    raw = str(value).strip()
    match = re.search(r"/folders/([A-Za-z0-9_-]+)", raw)
    if match:
        return match.group(1)
    match = re.search(r"[?&]id=([A-Za-z0-9_-]+)", raw)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", raw):
        return raw
    raise ValueError("Informe a URL ou o ID de uma pasta válida do Google Drive.")


def build_drive_service(credentials_info: Mapping[str, Any]) -> DriveClient:
    """Cria um cliente somente leitura usando a conta de serviço do Sheets."""
    info = {str(key): value for key, value in credentials_info.items()}
    if "private_key" in info:
        info["private_key"] = str(info["private_key"]).replace("\\n", "\n")
    required = {"client_email", "private_key", "token_uri"}
    missing = sorted(key for key in required if not str(info.get(key, "")).strip())
    if missing:
        raise ValueError(
            "As credenciais da conexão gsheets não contêm: " + ", ".join(missing)
        )
    credentials = service_account.Credentials.from_service_account_info(
        info, scopes=[DRIVE_READONLY_SCOPE]
    )
    return DriveClient(credentials)


def _drive_track_id(file_id: str) -> str:
    value = f"drive-track:{file_id}"
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:20]


def _remote_timestamp(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _cache_path(cache_dir: Path, file_id: str, name: str) -> Path:
    suffix = Path(name).suffix.casefold()
    if suffix not in AUDIO_EXTENSIONS:
        suffix = ".audio"
    return cache_dir / "drive_audio" / f"{file_id}{suffix}"


def ensure_drive_file(
    service: Any,
    file_metadata: Mapping[str, Any],
    cache_dir: Path,
) -> Path:
    """Mantém uma cópia de cache atualizada para metadados e reprodução."""
    file_id = str(file_metadata.get("id") or file_metadata.get("drive_file_id") or "")
    name = str(file_metadata.get("name") or file_metadata.get("drive_name") or "")
    if not file_id or not name:
        raise ValueError("O item do Google Drive não possui ID ou nome.")

    target = _cache_path(cache_dir, file_id, name)
    expected_size = int(float(file_metadata.get("size") or file_metadata.get("size_bytes") or 0))
    modified_at = file_metadata.get("modifiedTime") or file_metadata.get("modified_at")
    expected_mtime = _remote_timestamp(modified_at)
    if target.is_file():
        size_matches = not expected_size or target.stat().st_size == expected_size
        time_matches = (
            expected_mtime is None
            or abs(target.stat().st_mtime - expected_mtime) < 1
        )
        if size_matches and time_matches:
            return target

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.part")
    try:
        service.download(file_id, temporary)
        temporary.replace(target)
        if expected_mtime is not None:
            os.utime(target, (expected_mtime, expected_mtime))
        return target
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        raise ValueError(
            f"Não foi possível baixar '{name}' do Google Drive: {exc}"
        ) from exc


def _folder_items(service: Any, folder_id: str) -> list[dict[str, Any]]:
    return service.children(folder_id)


def list_drive_audio(service: Any, folder_value: str) -> tuple[str, list[dict[str, Any]]]:
    folder_id = extract_folder_id(folder_value)
    try:
        root = service.metadata(folder_id)
        if root.get("mimeType") != FOLDER_MIME_TYPE:
            raise ValueError("O link informado não corresponde a uma pasta do Google Drive.")

        audio_files: list[dict[str, Any]] = []
        pending = [(folder_id, str(root.get("name") or "Google Drive"))]
        while pending:
            current_id, relative_folder = pending.pop()
            for item in _folder_items(service, current_id):
                if item.get("mimeType") == FOLDER_MIME_TYPE:
                    pending.append(
                        (str(item["id"]), f"{relative_folder}/{item['name']}")
                    )
                    continue
                if Path(str(item.get("name", ""))).suffix.casefold() not in AUDIO_EXTENSIONS:
                    continue
                item["relative_folder"] = relative_folder
                audio_files.append(item)
        audio_files.sort(key=lambda item: str(item.get("name", "")).casefold())
        return folder_id, audio_files
    except ValueError as exc:
        raise ValueError(
            "Não foi possível acessar a pasta do Google Drive. Compartilhe-a com "
            f"a conta de serviço configurada no app. Detalhe: {exc}"
        ) from exc


def scan_drive_music(
    service: Any,
    folder_value: str,
    cache_dir: Path,
    progress: Callable[[int, int, str], None] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    folder_id, files = list_drive_audio(service, folder_value)
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, item in enumerate(files, 1):
        name = str(item.get("name") or "")
        try:
            cached_path = ensure_drive_file(service, item, cache_dir)
            relative_folder = str(item.get("relative_folder") or "Google Drive")
            default_album = relative_folder.rsplit("/", 1)[-1]
            row = read_audio(
                cached_path,
                cache_dir / "drive_audio",
                cache_dir,
                display_name=name,
                default_album=default_album,
                cover_cache_key=_drive_track_id(str(item["id"])),
            )
            row.update(
                {
                    "id": _drive_track_id(str(item["id"])),
                    "path": "",
                    "source": "google_drive",
                    "drive_file_id": str(item["id"]),
                    "drive_name": name,
                    "modified_at": str(item.get("modifiedTime") or ""),
                    "size_bytes": int(float(item.get("size") or 0)),
                    "library_root": f"google_drive:{folder_id}",
                    "available": "TRUE",
                }
            )
            rows.append(row)
        except (OSError, ValueError):
            errors.append(name)
        if progress:
            progress(index, len(files), name)
    if files and not rows:
        raise ValueError(
            "Nenhum arquivo do Google Drive pôde ser lido. Verifique as permissões "
            "e os formatos dos arquivos."
        )
    return folder_id, rows
