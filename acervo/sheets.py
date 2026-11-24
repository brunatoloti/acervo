from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd


TRACK_HEADERS = [
    "id", "path", "source", "drive_file_id", "drive_name",
    "title", "artist", "album", "album_artist",
    "track_number", "disc_number", "year", "genre", "duration_seconds",
    "cover_path", "modified_at", "size_bytes", "library_root", "available",
]
SETTING_HEADERS = ["key", "value", "updated_at"]
FAVORITE_HEADERS = ["owner_id", "kind", "item_id", "favorite", "updated_at"]
PROGRESS_HEADERS = [
    "owner_id", "item_id", "position_seconds", "duration_seconds",
    "completed", "updated_at",
]
WORKSHEETS = {
    "musicas": TRACK_HEADERS,
    "configuracoes": SETTING_HEADERS,
    "favoritos": FAVORITE_HEADERS,
    "progresso": PROGRESS_HEADERS,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty(headers: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=headers)


def _missing_worksheet(exc: Exception) -> bool:
    return exc.__class__.__name__ in {"WorksheetNotFound", "SpreadsheetNotFound"}


def read_worksheet(conn: Any, worksheet: str, headers: list[str]) -> pd.DataFrame:
    """Lê uma aba pelo GSheetsConnection e normaliza o esquema esperado."""
    try:
        frame = conn.read(worksheet=worksheet, ttl=0)
    except Exception as exc:
        if not _missing_worksheet(exc):
            raise
        return _empty(headers)

    if frame is None or frame.empty:
        return _empty(headers)
    frame = frame.copy()
    for column in headers:
        if column not in frame.columns:
            frame[column] = ""
    return frame[headers].copy()


def read_users(conn: Any) -> pd.DataFrame:
    """Lê a aba users usada pelos outros dashboards."""
    try:
        frame = conn.read(worksheet="users", ttl=0)
    except Exception as exc:
        if not _missing_worksheet(exc):
            raise
        return pd.DataFrame()
    return pd.DataFrame() if frame is None else frame.dropna(how="all").copy()


class GoogleSheetsStore:
    """Repositório baseado no st-gsheets-connection, sem gspread direto."""

    def __init__(self, connection: Any, owner_id: str):
        self.connection = connection
        self.owner_id = str(owner_id).strip().lower()

    def _frame(self, title: str) -> pd.DataFrame:
        return read_worksheet(self.connection, title, WORKSHEETS[title])

    def _records(self, title: str) -> list[dict[str, Any]]:
        frame = self._frame(title)
        if frame.empty:
            return []
        clean = frame.astype(object).where(pd.notna(frame), "")
        return clean.to_dict(orient="records")

    def _replace(self, title: str, rows: list[dict[str, Any]]) -> None:
        frame = pd.DataFrame(rows, columns=WORKSHEETS[title])
        try:
            self.connection.update(worksheet=title, data=frame)
        except Exception as exc:
            if not _missing_worksheet(exc):
                raise
            self.connection.create(worksheet=title, data=frame)

    def tracks(self) -> list[dict[str, Any]]:
        return self._records("musicas")

    def settings(self) -> dict[str, str]:
        return {
            str(row["key"]): str(row["value"])
            for row in self._records("configuracoes") if row.get("key")
        }

    def favorites(self) -> set[tuple[str, str]]:
        return {
            (str(row.get("kind")), str(row.get("item_id")))
            for row in self._records("favoritos")
            if str(row.get("owner_id", "")).strip().lower() == self.owner_id
            and str(row.get("favorite", "")).upper() == "TRUE"
        }

    def progress(self) -> dict[str, dict[str, Any]]:
        return {
            str(row["item_id"]): row
            for row in self._records("progresso")
            if row.get("item_id")
            and str(row.get("owner_id", "")).strip().lower() == self.owner_id
        }

    def sync_catalog(
        self,
        title: str,
        new_rows: list[dict[str, Any]],
        root: str,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        existing = {
            str(row.get("id")): row
            for row in self._records(title) if row.get("id")
        }
        scanned_ids = {str(row["id"]) for row in new_rows}
        for item_id, row in existing.items():
            row_source = str(row.get("source") or "local")
            same_catalog = (
                row_source == source if source else str(row.get("library_root")) == root
            )
            if same_catalog and item_id not in scanned_ids:
                row["available"] = "FALSE"
        for row in new_rows:
            existing[str(row["id"])] = row
        ordered = sorted(
            existing.values(), key=lambda row: str(row.get("title", "")).casefold()
        )
        self._replace(title, ordered)
        return ordered

    def set_setting(self, key: str, value: str) -> None:
        values = self.settings()
        values[key] = value
        rows = [
            {"key": item_key, "value": item_value, "updated_at": _now()}
            for item_key, item_value in sorted(values.items())
        ]
        self._replace("configuracoes", rows)

    def set_favorite(self, kind: str, item_id: str, favorite: bool) -> None:
        rows = self._records("favoritos")
        current = next(
            (row for row in rows
             if str(row.get("owner_id", "")).strip().lower() == self.owner_id
             and str(row.get("kind")) == kind
             and str(row.get("item_id")) == item_id),
            None,
        )
        payload = {
            "owner_id": self.owner_id, "kind": kind, "item_id": item_id,
            "favorite": "TRUE" if favorite else "FALSE", "updated_at": _now(),
        }
        if current is None:
            rows.append(payload)
        else:
            current.update(payload)
        self._replace("favoritos", rows)

    def set_progress(
        self, item_id: str, position: float, duration: float,
        completed: bool = False,
    ) -> None:
        rows = self._records("progresso")
        current = next(
            (row for row in rows
             if str(row.get("owner_id", "")).strip().lower() == self.owner_id
             and str(row.get("item_id")) == item_id),
            None,
        )
        payload = {
            "owner_id": self.owner_id,
            "item_id": item_id,
            "position_seconds": round(float(position), 2),
            "duration_seconds": round(float(duration), 2),
            "completed": "TRUE" if completed else "FALSE",
            "updated_at": _now(),
        }
        if current is None:
            rows.append(payload)
        else:
            current.update(payload)
        self._replace("progresso", rows)


