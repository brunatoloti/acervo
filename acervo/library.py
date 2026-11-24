from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".mp4a", ".ogg", ".opus", ".wav", ".aac"}


def _identity_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _identity_number(value: Any) -> int:
    try:
        return int(round(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def track_identity(track: dict[str, Any]) -> tuple[Any, ...]:
    """Identifica a mesma faixa independentemente da origem do arquivo."""
    return (
        _identity_text(track.get("album_artist") or track.get("artist")),
        _identity_text(track.get("album")),
        _identity_number(track.get("disc_number")),
        _identity_number(track.get("track_number")),
        _identity_text(track.get("title")),
        _identity_number(track.get("duration_seconds")),
    )


def deduplicate_cross_source_tracks(
    tracks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Prefere o Drive quando a mesma faixa também existe na pasta local."""
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    order: list[tuple[Any, ...]] = []
    for track in tracks:
        identity = track_identity(track)
        if identity not in groups:
            groups[identity] = []
            order.append(identity)
        groups[identity].append(track)

    result: list[dict[str, Any]] = []
    for identity in order:
        matches = groups[identity]
        sources = {str(track.get("source") or "local") for track in matches}
        if "google_drive" in sources and "local" in sources:
            result.append(
                next(
                    track
                    for track in matches
                    if str(track.get("source")) == "google_drive"
                )
            )
        else:
            result.extend(matches)
    return result


def resolve_library_path(value: str) -> Path:
    """Converte automaticamente caminhos do Windows quando o app roda no WSL."""
    raw = str(value).strip().strip('"')
    if os.name != "nt":
        windows_path = re.match(r"^([A-Za-z]):[\\/](.*)$", raw)
        if windows_path:
            drive = windows_path.group(1).lower()
            relative = re.sub(r"[\\/]+", "/", windows_path.group(2))
            return Path("/mnt") / drive / relative
    return Path(raw).expanduser()


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, (list, tuple)):
        value = value[0] if value else default
    return str(value).strip() or default


def _number(value: Any, default: int = 0) -> int:
    match = re.search(r"\d+", _text(value))
    return int(match.group()) if match else default


def _stable_id(kind: str, path: Path) -> str:
    identity = f"{kind}:{str(path.resolve()).casefold()}"
    return hashlib.sha1(identity.encode("utf-8")).hexdigest()[:20]


def _iso_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def _find_image(folder: Path) -> str:
    preferred = [
        "cover.jpg",
        "folder.jpg",
        "front.jpg",
        "cover.png",
    ]
    for name in preferred:
        candidate = folder / name
        if candidate.is_file():
            return str(candidate.resolve())
    return ""


def _extract_embedded_cover(audio: Any, output: Path) -> str:
    data: bytes | None = None
    suffix = ".jpg"

    pictures = getattr(audio, "pictures", None)
    if pictures:
        picture = pictures[0]
        data = picture.data
        suffix = ".png" if "png" in getattr(picture, "mime", "").lower() else ".jpg"
    elif getattr(audio, "tags", None):
        tags = audio.tags
        apic = next((value for key, value in tags.items() if str(key).startswith("APIC")), None)
        if apic is not None and hasattr(apic, "data"):
            data = apic.data
            suffix = ".png" if "png" in getattr(apic, "mime", "").lower() else ".jpg"
        elif "covr" in tags and tags["covr"]:
            cover = tags["covr"][0]
            data = bytes(cover)
            suffix = ".png" if getattr(cover, "imageformat", 0) == 14 else ".jpg"

    if not data:
        return ""
    target = output.with_suffix(suffix)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists() or target.stat().st_size != len(data):
        target.write_bytes(data)
    return str(target.resolve())


def read_audio(
    path: Path,
    root: Path,
    cache_dir: Path,
    display_name: str | None = None,
    default_album: str | None = None,
    cover_cache_key: str | None = None,
) -> dict[str, Any]:
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(path, easy=False)
    except Exception:
        audio = None

    tags = getattr(audio, "tags", {}) or {}

    def tag(*names: str, default: str = "") -> str:
        for name in names:
            if name in tags:
                return _text(tags[name], default)
            # Easy tags and ID3 frames have different names/casing.
            for key, value in tags.items():
                if str(key).casefold() == name.casefold():
                    return _text(getattr(value, "text", value), default)
        return default

    fallback_title = Path(display_name).stem if display_name else path.stem
    title = tag("title", "TIT2", "\xa9nam", default=fallback_title)
    artist = tag("artist", "TPE1", "\xa9ART", default="Artista desconhecido")
    album = tag(
        "album", "TALB", "\xa9alb", default=default_album or path.parent.name
    )
    album_artist = tag("albumartist", "album artist", "TPE2", "aART", default=artist)
    track_number = _number(tag("tracknumber", "TRCK", "trkn"))
    disc_number = _number(tag("discnumber", "TPOS", "disk"), 1)
    date = tag("date", "year", "TDRC", "\xa9day")
    genre = tag("genre", "TCON", "\xa9gen")
    duration = round(float(getattr(getattr(audio, "info", None), "length", 0) or 0), 2)

    cover = _find_image(path.parent)
    if not cover and audio is not None:
        cache_key = cover_cache_key or _stable_id("album", path.parent)
        cover = _extract_embedded_cover(
            audio, cache_dir / "covers" / cache_key
        )

    stat = path.stat()
    return {
        "id": _stable_id("track", path),
        "path": str(path.resolve()),
        "source": "local",
        "drive_file_id": "",
        "drive_name": "",
        "title": title,
        "artist": artist,
        "album": album,
        "album_artist": album_artist,
        "track_number": track_number,
        "disc_number": disc_number,
        "year": _number(date),
        "genre": genre,
        "duration_seconds": duration,
        "cover_path": cover,
        "modified_at": _iso_mtime(path),
        "size_bytes": stat.st_size,
        "library_root": str(root.resolve()),
        "available": "TRUE",
    }


def _media_files(root: Path, extensions: set[str]) -> Iterable[Path]:
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.casefold() in extensions:
            yield path


def scan_music(
    root_value: str,
    cache_dir: Path,
    progress: Callable[[int, int, str], None] | None = None,
) -> list[dict[str, Any]]:
    root = resolve_library_path(root_value)
    if not root.is_dir():
        raise ValueError(f"A pasta de músicas não existe: {root}")
    files = list(_media_files(root, AUDIO_EXTENSIONS))
    rows = []
    for index, path in enumerate(files, 1):
        try:
            rows.append(read_audio(path, root, cache_dir))
        except (OSError, ValueError):
            continue
        if progress:
            progress(index, len(files), path.name)
    return rows


def duration_label(seconds: Any) -> str:
    try:
        value = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        value = 0
    hours, remainder = divmod(value, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"

