from __future__ import annotations

import base64
import hashlib
import html
import json
import mimetypes
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import streamlit as st
from streamlit_gsheets import GSheetsConnection

from acervo.shelf import (
    build_drive_service,
    ensure_drive_file,
    scan_drive_music,
)
from acervo.library import (
    deduplicate_cross_source_tracks,
    duration_label,
    resolve_library_path,
    scan_music,
)
from acervo.sheets import GoogleSheetsStore
from auth import require_login


APP_ROOT = Path(__file__).parent
CACHE_DIR = APP_ROOT / ".acervo_cache"
AUDIO_MIME = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
}
SOURCE_LABELS = {
    "google_drive": "Google Drive",
    "local": "Pasta local",
}

AUDIO_CONTROL_JS = """
export default function(component) {
  const { data, setStateValue } = component;
  let attempts = 0;
  let player = null;
  let cancelled = false;
  let persist = null;
  let ended = null;
  let restore = null;
  let keyboardShortcut = null;
  const controllerId = Symbol('acervo-media-controller');
  const mediaActions = ['play', 'pause', 'previoustrack', 'nexttrack'];
  const storageKey = `acervo-audio-position:${data.trackId}`;
  const sendAction = (action) => setStateValue('action', {
    action,
    token: `${Date.now()}-${Math.random()}`
  });
  const setMediaAction = (action, handler) => {
    try {
      navigator.mediaSession.setActionHandler(action, handler);
    } catch (_) {
      // Alguns navegadores implementam apenas parte da Media Session API.
    }
  };
  const configureKeyboardShortcuts = () => {
    keyboardShortcut = (event) => {
      const target = event.target;
      const isEditing = target instanceof HTMLElement && (
        target.isContentEditable ||
        ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)
      );
      if (isEditing || event.repeat || !(event.ctrlKey || event.metaKey)) return;
      if (event.key === 'ArrowLeft' && data.hasPrevious) {
        event.preventDefault();
        sendAction('previous');
      } else if (event.key === 'ArrowRight' && data.hasNext) {
        event.preventDefault();
        sendAction('next');
      }
    };
    window.addEventListener('keydown', keyboardShortcut);
  };
  const configureMediaSession = () => {
    if (!('mediaSession' in navigator)) return;
    window.__acervoMediaController = controllerId;
    if ('MediaMetadata' in window) {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: data.title || '',
        artist: data.artist || '',
        album: data.album || ''
      });
    }
    navigator.mediaSession.playbackState = data.paused ? 'paused' : 'playing';
    setMediaAction('play', () => {
      player.play().catch(() => {});
      navigator.mediaSession.playbackState = 'playing';
      sendAction('play');
    });
    setMediaAction('pause', () => {
      player.pause();
      navigator.mediaSession.playbackState = 'paused';
      sendAction('pause');
    });
    setMediaAction('previoustrack', data.hasPrevious ? () => sendAction('previous') : null);
    setMediaAction('nexttrack', data.hasNext ? () => sendAction('next') : null);
  };
  const applyCommand = () => {
    const players = document.querySelectorAll('audio');
    player = players.length ? players[players.length - 1] : null;
    if (!player && attempts++ < 20) {
      window.setTimeout(applyCommand, 50);
      return;
    }
    if (!player || cancelled) return;
    if (data.reset) sessionStorage.removeItem(storageKey);
    const saved = data.reset ? 0 : Number(sessionStorage.getItem(storageKey) || 0);
    persist = () => {
      if (Number.isFinite(player.currentTime) && player.currentTime > 0) {
        sessionStorage.setItem(storageKey, String(player.currentTime));
      }
    };
    ended = () => sessionStorage.setItem(storageKey, '0');
    restore = () => {
      if (cancelled) return;
      if (data.reset) {
        player.currentTime = 0;
      } else if (Number.isFinite(saved) && saved > 0 && Math.abs(player.currentTime - saved) > 1) {
        player.currentTime = saved;
      }
      if (data.paused) {
        player.pause();
      } else {
        player.play().catch(() => {});
      }
    };
    player.addEventListener('timeupdate', persist);
    player.addEventListener('pause', persist);
    player.addEventListener('ended', ended);
    if (player.readyState >= 1) restore();
    else player.addEventListener('loadedmetadata', restore, { once: true });
    configureKeyboardShortcuts();
    configureMediaSession();
  };
  applyCommand();
  return () => {
    cancelled = true;
    if ('mediaSession' in navigator && window.__acervoMediaController === controllerId) {
      mediaActions.forEach((action) => setMediaAction(action, null));
      navigator.mediaSession.metadata = null;
      navigator.mediaSession.playbackState = 'none';
      delete window.__acervoMediaController;
    }
    if (keyboardShortcut) {
      window.removeEventListener('keydown', keyboardShortcut);
    }
    if (!player) return;
    if (persist) {
      persist();
      player.removeEventListener('timeupdate', persist);
      player.removeEventListener('pause', persist);
    }
    if (ended) player.removeEventListener('ended', ended);
    if (restore) player.removeEventListener('loadedmetadata', restore);
  };
}
"""
AUDIO_CONTROLLER = (
    st.components.v2.component("acervo_audio_controller", js=AUDIO_CONTROL_JS)
    if hasattr(st.components, "v2")
    else None
)

ALBUM_CARD_HTML = """
<button class="album-card" type="button">
  <div class="album-artwork">
    <img alt="" hidden>
    <span class="album-placeholder">♫</span>
  </div>
  <div class="album-info">
    <div class="album-title"></div>
    <div class="album-caption"></div>
  </div>
</button>
"""
ALBUM_CARD_CSS = """
* { box-sizing: border-box; }
.album-card { all: unset; color: #fff; cursor: pointer; display: block; font-family: inherit; width: 100%; }
.album-artwork { align-items: center; aspect-ratio: 1 / 1; background: linear-gradient(135deg, #333, #151515); border-radius: 8px; color: #777; display: flex; font-size: 3rem; justify-content: center; overflow: hidden; transition: filter .2s, transform .2s; width: 100%; }
.album-artwork img { display: block; height: 100%; object-fit: cover; width: 100%; }
.album-info { background: #181818; border-radius: 10px; min-height: 94px; padding: 1rem; margin-top: .2rem; transition: background .2s, transform .2s; }
.album-title { font-size: 1rem; font-weight: 750; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.album-caption { color: #a7a7a7; font-size: .84rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.album-card:hover .album-artwork { filter: brightness(1.08); transform: translateY(-2px); }
.album-card:hover .album-info { background: #242424; transform: translateY(-2px); }
.album-card:focus-visible { border-radius: 10px; outline: 2px solid #a855f7; outline-offset: 3px; }
"""
ALBUM_CARD_JS = """
export default function(component) {
  const { data, setStateValue, parentElement } = component;
  const card = parentElement.querySelector('.album-card');
  const image = card.querySelector('img');
  const placeholder = card.querySelector('.album-placeholder');
  card.querySelector('.album-title').textContent = data.title || 'Sem título';
  card.querySelector('.album-caption').textContent = data.caption || '';
  card.setAttribute('aria-label', `Abrir álbum ${data.title || ''}`);
  if (data.cover) {
    image.src = data.cover;
    image.alt = `Capa de ${data.title || 'álbum'}`;
    image.hidden = false;
    placeholder.hidden = true;
  } else {
    image.hidden = true;
    placeholder.hidden = false;
  }
  const openAlbum = () => setStateValue('selected', {
    albumId: data.albumId,
    token: `${Date.now()}-${Math.random()}`
  });
  card.addEventListener('click', openAlbum);
  return () => card.removeEventListener('click', openAlbum);
}
"""
ALBUM_CARD_COMPONENT = (
    st.components.v2.component(
        "acervo_album_card",
        html=ALBUM_CARD_HTML,
        css=ALBUM_CARD_CSS,
        js=ALBUM_CARD_JS,
    )
    if hasattr(st.components, "v2")
    else None
)

st.set_page_config(
    page_title="Acervo",
    page_icon="◉",
    layout="wide",
    initial_sidebar_state="expanded",
)


def inject_theme() -> None:
    st.markdown(
        """
        <style>
        :root { --acervo-purple: #a855f7; --acervo-panel: #181818; --acervo-muted: #a7a7a7; }
        .stApp { background: linear-gradient(180deg, #202020 0, #121212 300px); color: #fff; }
        [data-testid="stSidebar"] { background: #090909; }
        [data-testid="stSidebar"] hr { border-color: #282828; }
        h1, h2, h3 { letter-spacing: -.03em; }
        .hero { padding: 1.1rem 0 1.4rem; }
        .eyebrow { color: var(--acervo-purple); font-size: .78rem; font-weight: 800; letter-spacing: .13em; text-transform: uppercase; }
        .hero-title { font-size: clamp(2.3rem, 5vw, 4.8rem); font-weight: 850; line-height: .98; margin: .25rem 0 .6rem; }
        .muted { color: var(--acervo-muted); }
        .media-card { background: #181818; border-radius: 10px; min-height: 94px; padding: 1rem; margin: .2rem 0 .45rem; transition: background .2s, transform .2s; }
        .media-card:hover { background: #242424; transform: translateY(-2px); }
        .media-card .title { font-size: 1rem; font-weight: 750; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .media-card .caption { color: var(--acervo-muted); font-size: .84rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .placeholder { align-items: center; background: linear-gradient(135deg, #333, #151515); border-radius: 8px; color: #777; display: flex; font-size: 3rem; justify-content: center; min-height: 180px; }
        [data-testid="stMetric"] { background: rgba(24,24,24,.82); border: 1px solid #2b2b2b; border-radius: 12px; padding: 1rem; }
        .track-row { border-bottom: 1px solid #252525; padding: .15rem 0; }
        [class*="st-key-playing_track_"] { background: rgba(168,85,247,.09); border: 1px solid rgba(168,85,247,.2); border-radius: 10px; padding: .35rem .55rem; }
        [class*="st-key-paused_track_"] { background: rgba(255,255,255,.045); border: 1px solid #343434; border-radius: 10px; padding: .35rem .55rem; }
        .track-title { font-size: .96rem; font-weight: 750; }
        .track-title.playing { color: var(--acervo-purple); }
        .track-title.paused { color: #d5d5d5; }
        .playing-state { align-items: center; color: var(--acervo-purple); display: inline-flex; font-size: .68rem; font-weight: 800; gap: .4rem; letter-spacing: .08em; margin-left: .75rem; text-transform: uppercase; }
        .playing-bars { align-items: end; display: inline-flex; gap: 2px; height: 13px; }
        .playing-bars i { animation: acervo-bars .8s ease-in-out infinite alternate; background: var(--acervo-purple); border-radius: 2px; display: block; height: 35%; width: 3px; }
        .playing-bars i:nth-child(2) { animation-delay: -.35s; height: 90%; }
        .playing-bars i:nth-child(3) { animation-delay: -.6s; height: 60%; }
        @keyframes acervo-bars { from { height: 25%; } to { height: 100%; } }
        .st-key-now_playing { background: rgba(20,20,20,.97); border: 1px solid #303030; border-radius: 14px; padding: .8rem; position: sticky; bottom: .75rem; z-index: 20; box-shadow: 0 12px 38px #000b; }
        [data-testid="stSidebar"] .st-key-now_playing { margin-top: 1rem; }
        .player-placeholder { align-items: center; aspect-ratio: 1 / 1; background: linear-gradient(135deg, #333, #151515); border-radius: 7px; color: #777; display: flex; font-size: 1.7rem; justify-content: center; width: 100%; }
        .player-title { font-size: .92rem; font-weight: 750; line-height: 1.2; margin-bottom: .25rem; overflow: hidden; text-overflow: ellipsis; }
        .player-caption { color: var(--acervo-muted); font-size: .76rem; line-height: 1.25; overflow: hidden; text-overflow: ellipsis; }
        .setup { background: #181818; border: 1px solid #333; border-radius: 12px; padding: 1rem 1.25rem; }
        .login-brand { text-align: center; padding: 8vh 0 1.2rem; }
        .login-logo { color: var(--acervo-purple); font-size: 4rem; line-height: 1; margin-bottom: 1rem; }
        .login-brand h1 { margin: .35rem 0; }
        .stButton > button { border-radius: 999px; font-weight: 700; }
        .stButton > button[kind="primary"] { background: var(--acervo-purple); border-color: var(--acervo-purple); color: #160522; }
        div[data-testid="stImage"] img { border-radius: 8px; aspect-ratio: 1 / 1; object-fit: cover; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def truthy(value: Any) -> bool:
    return str(value).strip().upper() not in {"", "0", "FALSE", "NÃO", "NO", "NONE"}


def available(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # O scanner já mantém este estado. Fazer Path.is_file() para milhares de
    # itens do OneDrive em cada rerun deixa qualquer interação muito lenta.
    return [row for row in rows if truthy(row.get("available", True))]


def track_source(track: dict[str, Any]) -> str:
    source = str(track.get("source") or "local")
    return source if source in SOURCE_LABELS else "local"


def library_tracks() -> list[dict[str, Any]]:
    return deduplicate_cross_source_tracks(available(st.session_state.tracks))


def year_label(value: Any) -> str:
    try:
        number = int(float(value))
        return str(number) if number else ""
    except (TypeError, ValueError):
        return ""


def safe(value: Any) -> str:
    return html.escape(str(value or ""))


def get_connection() -> GSheetsConnection:
    """Mesmo padrão usado no reading-dashboard e nos demais projetos."""
    try:
        return st.connection("gsheets", type=GSheetsConnection)
    except Exception as exc:
        st.error(
            "Não foi possível acessar a planilha. Confira a conexão 'gsheets' "
            f"nos Secrets do Streamlit. Detalhe: {exc}"
        )
        st.stop()


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    return value


def drive_credentials_info() -> dict[str, Any]:
    try:
        connection_secrets = _plain(st.secrets["connections"]["gsheets"])
    except Exception as exc:
        raise ValueError(
            "A conexão 'gsheets' não foi encontrada nos Secrets do Streamlit."
        ) from exc
    service_account_fields = {
        "type", "project_id", "private_key_id", "private_key",
        "client_email", "client_id", "auth_uri", "token_uri",
        "auth_provider_x509_cert_url", "client_x509_cert_url",
        "universe_domain",
    }
    return {
        key: value
        for key, value in connection_secrets.items()
        if key in service_account_fields
    }


@st.cache_resource(show_spinner=False)
def cached_drive_service(serialized_credentials: str) -> Any:
    return build_drive_service(json.loads(serialized_credentials))


def get_drive_service() -> Any:
    serialized = json.dumps(
        drive_credentials_info(), sort_keys=True, ensure_ascii=True
    )
    return cached_drive_service(serialized)


def load_library(store: GoogleSheetsStore, force: bool = False) -> None:
    owner_changed = st.session_state.get("library_owner") != store.owner_id
    if force or owner_changed or "tracks" not in st.session_state:
        if owner_changed:
            for key in (
                "selected_album", "current_track_id", "queue_ids",
            ):
                st.session_state.pop(key, None)
        with st.spinner("Carregando seu acervo…"):
            st.session_state.tracks = store.tracks()
        st.session_state.settings = {}
        st.session_state.favorites = set()
        st.session_state.pop("settings_owner", None)
        st.session_state.pop("favorites_owner", None)
        st.session_state.library_owner = store.owner_id


def load_settings(store: GoogleSheetsStore) -> None:
    if st.session_state.get("settings_owner") != store.owner_id:
        st.session_state.settings = store.settings()
        st.session_state.settings_owner = store.owner_id


def load_favorites(store: GoogleSheetsStore) -> None:
    if st.session_state.get("favorites_owner") != store.owner_id:
        st.session_state.favorites = store.favorites()
        st.session_state.favorites_owner = store.owner_id


def refresh_catalog(
    store: GoogleSheetsStore,
    progress: Any,
) -> tuple[list[str], list[str]]:
    settings = store.settings()
    refreshed: list[str] = []
    errors: list[str] = []
    catalog = store.tracks()

    drive_folder = settings.get("drive_folder", "").strip()
    if drive_folder:
        try:
            service = get_drive_service()
            folder_id, rows = scan_drive_music(
                service, drive_folder, CACHE_DIR, progress
            )
            catalog = store.sync_catalog(
                "musicas",
                rows,
                f"google_drive:{folder_id}",
                source="google_drive",
            )
            refreshed.append("Google Drive")
        except Exception as exc:
            errors.append(f"Google Drive: {exc}")

    music_root = settings.get("music_root", "").strip()
    if music_root:
        try:
            rows = scan_music(music_root, CACHE_DIR, progress)
            normalized_root = str(resolve_library_path(music_root).resolve())
            catalog = store.sync_catalog(
                "musicas", rows, normalized_root, source="local"
            )
            refreshed.append("pasta local")
        except Exception as exc:
            errors.append(f"Pasta local: {exc}")

    st.session_state.tracks = catalog
    st.session_state.settings = settings
    st.session_state.settings_owner = store.owner_id
    st.session_state.library_owner = store.owner_id
    return refreshed, errors


def sidebar_header(display_name: str) -> None:
    with st.sidebar:
        st.markdown("# ◉ Acervo")
        st.caption(f"Olá, {display_name}")


def sidebar_navigation(pages: list[tuple[Any, str, str]]) -> None:
    with st.sidebar:
        for page, label, icon in pages:
            st.page_link(
                page,
                label=label,
                icon=icon,
                use_container_width=True,
            )


def sidebar_footer(store: GoogleSheetsStore) -> None:
    with st.sidebar:
        st.divider()
        if st.button("Atualizar dados", use_container_width=True):
            callback, widgets = scan_progress()
            refreshed, errors = refresh_catalog(store, callback)
            widgets[0].empty()
            widgets[1].empty()
            if refreshed:
                message = "Dados e capas atualizados: " + " e ".join(refreshed) + "."
                level = "warning" if errors else "success"
                if errors:
                    message += " Não foi possível atualizar " + " | ".join(errors)
                st.session_state.catalog_refresh_notice = (level, message)
                st.rerun()
            elif errors:
                st.error("Não foi possível atualizar o acervo. " + " | ".join(errors))
            else:
                load_library(store, force=True)
                st.session_state.catalog_refresh_notice = (
                    "info",
                    "Nenhuma pasta está configurada. Os dados salvos foram recarregados.",
                )
                st.rerun()
        notice = st.session_state.pop("catalog_refresh_notice", None)
        if notice:
            getattr(st, notice[0])(notice[1])
        st.caption(f"{len(library_tracks())} faixas")


def image_or_placeholder(path_value: Any, icon: str, height: int = 180) -> None:
    path = Path(str(path_value or ""))
    if path.is_file():
        st.image(str(path), use_container_width=True)
    else:
        st.markdown(f'<div class="placeholder" style="min-height:{height}px">{icon}</div>', unsafe_allow_html=True)


@st.cache_data(show_spinner=False)
def cover_data_uri(path_value: str, modified_at: int) -> str:
    del modified_at
    path = Path(path_value)
    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def clickable_album_card(
    album: dict[str, Any], navigate_to_music: bool = False
) -> None:
    cover_path = Path(str(album.get("cover") or ""))
    if cover_path.is_file():
        cover = cover_data_uri(
            str(cover_path), cover_path.stat().st_mtime_ns
        )
    else:
        cover = ""
    caption = f"{album['artist']} · {album['year']}".rstrip(" ·")
    context = "home" if navigate_to_music else "library"
    if ALBUM_CARD_COMPONENT is None:
        image_or_placeholder(album.get("cover"), "♫")
        if st.button(
            str(album["title"]),
            key=f"album_fallback_{context}_{album['key']}",
            use_container_width=True,
        ):
            st.session_state.selected_album = str(album["key"])
            if navigate_to_music:
                st.switch_page(music_page)
            st.rerun()
        return

    result = ALBUM_CARD_COMPONENT(
        key=f"album_card_{context}_{album['key']}",
        data={
            "albumId": str(album["key"]),
            "title": str(album["title"]),
            "caption": caption,
            "cover": cover,
        },
        default={"selected": None},
        height="content",
        on_selected_change=lambda: None,
    )
    action = getattr(result, "selected", None)
    if not hasattr(action, "get") or not action.get("token"):
        return
    token_key = f"album_card_action_{context}_{album['key']}"
    if st.session_state.get(token_key) == action["token"]:
        return
    st.session_state[token_key] = action["token"]
    st.session_state.selected_album = str(action.get("albumId") or album["key"])
    if navigate_to_music:
        st.switch_page(music_page)
    st.rerun()


def album_groups(tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for track in tracks:
        key = (str(track.get("album_artist") or track.get("artist") or "Artista desconhecido"), str(track.get("album") or "Sem álbum"))
        grouped[key].append(track)
    albums = []
    for (artist, title), items in grouped.items():
        ordered = sorted(items, key=lambda item: (int(float(item.get("disc_number") or 1)), int(float(item.get("track_number") or 0)), str(item.get("title"))))
        identity = f"{artist}\0{title}"
        albums.append(
            {
                "key": hashlib.sha1(identity.encode("utf-8")).hexdigest()[:20],
                "artist": artist,
                "title": title,
                "cover": next((item.get("cover_path") for item in ordered if item.get("cover_path")), ""),
                "year": next((year_label(item.get("year")) for item in ordered if year_label(item.get("year"))), ""),
                "tracks": ordered,
                "sources": sorted({track_source(item) for item in ordered}),
                "modified_at": max((str(item.get("modified_at", "")) for item in ordered), default=""),
            }
        )
    return sorted(albums, key=lambda album: (album["artist"].casefold(), album["title"].casefold()))


def play_track(track: dict[str, Any], queue: list[dict[str, Any]]) -> None:
    st.session_state.current_track_id = str(track["id"])
    st.session_state.queue_ids = [str(item["id"]) for item in queue]
    st.session_state.playback_paused = False
    st.session_state.audio_reset = True
    st.session_state.audio_command_nonce = st.session_state.get("audio_command_nonce", 0) + 1
    st.rerun()


def render_album(album: dict[str, Any], store: GoogleSheetsStore) -> None:
    if st.button("← Voltar aos álbuns"):
        st.session_state.pop("selected_album", None)
        st.rerun()
    left, right = st.columns([1, 3], vertical_alignment="bottom")
    with left:
        image_or_placeholder(album["cover"], "♫", 240)
    with right:
        st.caption("ÁLBUM")
        st.title(album["title"])
        st.write(f"**{album['artist']}** · {album['year'] or 'Ano desconhecido'} · {len(album['tracks'])} faixas")
        source_names = [SOURCE_LABELS[source] for source in album["sources"]]
        source_prefix = "Origem" if len(source_names) == 1 else "Origens"
        st.caption(f"{source_prefix}: {' e '.join(source_names)}")
        if st.button("▶ Tocar álbum", type="primary"):
            play_track(album["tracks"][0], album["tracks"])

    st.markdown("### Faixas")
    for index, track in enumerate(album["tracks"], 1):
        is_current = str(st.session_state.get("current_track_id", "")) == str(track["id"])
        is_paused = is_current and bool(st.session_state.get("playback_paused", False))
        is_playing = is_current and not is_paused
        if is_playing:
            row_key = f"playing_track_{track['id']}"
        elif is_paused:
            row_key = f"paused_track_{track['id']}"
        else:
            row_key = f"track_row_{track['id']}"
        with st.container(key=row_key):
            columns = st.columns([0.75, 5.5, 1, 0.75], vertical_alignment="center")
            if columns[0].button(
                "⏸" if is_playing else "▶",
                key=f"play_{track['id']}",
                help="Pausar faixa" if is_playing else ("Continuar faixa" if is_paused else "Tocar faixa"),
                use_container_width=True,
            ):
                if is_current:
                    st.session_state.playback_paused = not is_paused
                    st.session_state.audio_command_nonce = st.session_state.get("audio_command_nonce", 0) + 1
                    st.rerun()
                play_track(track, album["tracks"])
            if is_playing:
                state = (
                    '<span class="playing-state"><span class="playing-bars">'
                    '<i></i><i></i><i></i></span>Tocando</span>'
                )
            elif is_paused:
                state = '<span class="playing-state">Pausada</span>'
            else:
                state = ""
            title_class = "track-title playing" if is_playing else ("track-title paused" if is_paused else "track-title")
            columns[1].markdown(
                f'<div><span class="{title_class}">{safe(track.get("title"))}</span>{state}</div>'
                f'<div class="muted">{safe(track.get("artist"))}</div>',
                unsafe_allow_html=True,
            )
            columns[2].caption(duration_label(track.get("duration_seconds")))
            is_favorite = ("track", str(track["id"])) in st.session_state.favorites
            if columns[3].button(
                "♥" if is_favorite else "♡",
                key=f"fav_track_{track['id']}",
                help="Remover dos favoritos" if is_favorite else "Adicionar aos favoritos",
                use_container_width=True,
            ):
                store.set_favorite("track", str(track["id"]), not is_favorite)
                favorite_key = ("track", str(track["id"]))
                if is_favorite:
                    st.session_state.favorites.discard(favorite_key)
                else:
                    st.session_state.favorites.add(favorite_key)
                st.rerun()


def render_music(store: GoogleSheetsStore) -> None:
    load_favorites(store)
    tracks = library_tracks()
    albums = album_groups(tracks)
    selected = st.session_state.get("selected_album")
    album = next((item for item in albums if item["key"] == selected), None)
    if album:
        render_album(album, store)
        return

    st.markdown(
        '<div class="eyebrow">Sua coleção</div>',
        unsafe_allow_html=True,
    )
    st.title("Músicas")
    search_column, sort_column = st.columns([3, 1])
    query = search_column.text_input("Buscar", placeholder="Artista, álbum ou faixa…", label_visibility="collapsed")
    sort = sort_column.selectbox("Ordenar", ["Artista", "Álbum", "Mais recentes"], label_visibility="collapsed")
    query_lower = query.casefold().strip()
    if query_lower:
        albums = [
            album
            for album in albums
            if query_lower in f"{album['artist']} {album['title']} {' '.join(str(t.get('title', '')) for t in album['tracks'])}".casefold()
        ]
    if sort == "Álbum":
        albums.sort(key=lambda item: item["title"].casefold())
    elif sort == "Mais recentes":
        albums.sort(key=lambda item: item["modified_at"], reverse=True)

    if not albums:
        st.info("Nenhum álbum encontrado. Importe sua pasta em Configurações.")
        return
    page_size = 30
    total_pages = (len(albums) + page_size - 1) // page_size
    if total_pages > 1:
        page_number = st.selectbox(
            "Página dos álbuns",
            range(1, total_pages + 1),
            format_func=lambda value: f"Página {value} de {total_pages}",
            label_visibility="collapsed",
        )
        start_index = (page_number - 1) * page_size
        albums = albums[start_index : start_index + page_size]
    for start in range(0, len(albums), 5):
        columns = st.columns(5)
        for column, album_item in zip(columns, albums[start : start + 5]):
            with column:
                clickable_album_card(album_item)


def render_home() -> None:
    tracks = library_tracks()
    albums = album_groups(tracks)
    artists = {str(track.get("album_artist") or track.get("artist")) for track in tracks}
    st.markdown(
        '<div class="hero"><div class="eyebrow">Google Drive e pasta local</div>'
        '<div class="hero-title">Toda a sua música,<br>em um só lugar.</div>'
        '</div>',
        unsafe_allow_html=True,
    )
    columns = st.columns(3)
    columns[0].metric("Faixas", len(tracks))
    columns[1].metric("Álbuns", len(albums))
    columns[2].metric("Músicos/bandas", len(artists))

    recent = sorted(albums, key=lambda item: item["modified_at"], reverse=True)[:5]
    if recent:
        st.markdown("## Álbuns adicionados recentemente")
        columns = st.columns(len(recent))
        for column, album in zip(columns, recent):
            with column:
                clickable_album_card(album, navigate_to_music=True)

def scan_progress() -> tuple[Any, Any]:
    bar = st.progress(0.0)
    status = st.empty()

    def update(current: int, total: int, name: str) -> None:
        bar.progress(current / max(total, 1))
        status.caption(f"Lendo {current} de {total}: {name}")

    return update, (bar, status)


def render_settings(store: GoogleSheetsStore, authenticator: Any) -> None:
    load_settings(store)
    st.markdown('<div class="eyebrow">Música</div>', unsafe_allow_html=True)
    st.title("Configurações")
    st.write(
        "As músicas do Google Drive e da pasta local aparecem juntas em uma "
        "única coleção."
    )

    st.markdown("### Google Drive")
    drive_folder = st.text_input(
        "Pasta de músicas do Google Drive",
        value=st.session_state.settings.get("drive_folder", ""),
        placeholder="https://drive.google.com/drive/folders/...",
    )
    if st.button(
        "Importar / atualizar Google Drive",
        type="primary",
        disabled=not bool(drive_folder.strip()),
    ):
        callback, widgets = scan_progress()
        try:
            service = get_drive_service()
            folder_id, rows = scan_drive_music(
                service, drive_folder.strip(), CACHE_DIR, callback
            )
            catalog = store.sync_catalog(
                "musicas",
                rows,
                f"google_drive:{folder_id}",
                source="google_drive",
            )
            store.set_setting("drive_folder", drive_folder.strip())
            st.session_state.tracks = catalog
            st.session_state.settings["drive_folder"] = drive_folder.strip()
            widgets[1].success(f"{len(rows)} faixas catalogadas no Google Drive.")
        except Exception as exc:
            widgets[1].error(str(exc))

    with st.expander("Pasta local (opcional)"):
        music_root = st.text_input(
            "Pasta local de músicas",
            value=st.session_state.settings.get("music_root", ""),
            placeholder=r"D:\Músicas\CDs",
        )
        if st.button(
            "Importar / atualizar pasta local",
            disabled=not bool(music_root.strip()),
        ):
            callback, widgets = scan_progress()
            try:
                rows = scan_music(music_root.strip(), CACHE_DIR, callback)
                normalized_root = str(resolve_library_path(music_root).resolve())
                catalog = store.sync_catalog(
                    "musicas", rows, normalized_root, source="local"
                )
                store.set_setting("music_root", music_root.strip())
                st.session_state.tracks = catalog
                st.session_state.settings["music_root"] = music_root.strip()
                widgets[1].success(f"{len(rows)} faixas locais catalogadas.")
            except Exception as exc:
                widgets[1].error(str(exc))

    st.divider()
    st.markdown("### Formatos recomendados")
    st.write("MP3, FLAC, M4A, OGG, Opus, WAV e AAC.")

    st.divider()
    st.markdown("### Conta")
    st.caption("Encerre sua sessão neste dispositivo.")
    authenticator.logout(
        button_name="Sair da conta",
        location="main",
        key="settings_logout",
        use_container_width=False,
    )


def skip_queue_track(
    queue_ids: list[str], current_index: int, offset: int
) -> None:
    target_index = current_index + offset
    if not 0 <= target_index < len(queue_ids):
        return
    st.session_state.current_track_id = queue_ids[target_index]
    st.session_state.playback_paused = False
    st.session_state.audio_reset = True
    st.session_state.audio_command_nonce = (
        st.session_state.get("audio_command_nonce", 0) + 1
    )
    st.rerun()


def render_player() -> None:
    track_id = st.session_state.get("current_track_id")
    if not track_id:
        return
    tracks = {str(track["id"]): track for track in library_tracks()}
    track = tracks.get(str(track_id))
    if not track:
        return
    queue_ids = [item_id for item_id in st.session_state.get("queue_ids", []) if item_id in tracks]
    current_index = queue_ids.index(str(track_id)) if str(track_id) in queue_ids else 0
    with st.sidebar:
        with st.container(key="now_playing"):
            st.caption("TOCANDO AGORA")
            cover_column, info_column = st.columns(
                [1, 2], vertical_alignment="center"
            )
            cover_path = Path(str(track.get("cover_path") or ""))
            with cover_column:
                if cover_path.is_file():
                    st.image(str(cover_path), use_container_width=True)
                else:
                    st.markdown(
                        '<div class="player-placeholder">♫</div>',
                        unsafe_allow_html=True,
                    )
            info_column.markdown(
                f'<div class="player-title">{safe(track.get("title"))}</div>'
                f'<div class="player-caption">{safe(track.get("artist"))}</div>'
                f'<div class="player-caption">{safe(track.get("album"))}</div>',
                unsafe_allow_html=True,
            )

            with st.container(
                horizontal=True,
                horizontal_alignment="center",
                gap="small",
            ):
                if st.button(
                    "Anterior",
                    key="player_previous",
                    icon=":material/skip_previous:",
                    type="tertiary",
                    disabled=current_index <= 0,
                    help="Faixa anterior",
                ):
                    skip_queue_track(queue_ids, current_index, -1)
                if st.button(
                    "Próxima",
                    key="player_next",
                    icon=":material/skip_next:",
                    type="tertiary",
                    disabled=current_index >= len(queue_ids) - 1,
                    help="Próxima faixa",
                ):
                    skip_queue_track(queue_ids, current_index, 1)

            try:
                if track_source(track) == "google_drive":
                    audio_path = ensure_drive_file(
                        get_drive_service(),
                        {
                            "drive_file_id": track.get("drive_file_id"),
                            "drive_name": track.get("drive_name"),
                            "size_bytes": track.get("size_bytes"),
                            "modified_at": track.get("modified_at"),
                        },
                        CACHE_DIR,
                    )
                else:
                    audio_path = Path(str(track["path"]))
                suffix = audio_path.suffix.casefold()
                paused = bool(st.session_state.get("playback_paused", False))
                st.audio(
                    str(audio_path),
                    format=AUDIO_MIME.get(suffix, "audio/mpeg"),
                    autoplay=not paused,
                )
                if AUDIO_CONTROLLER is not None:
                    controller_result = AUDIO_CONTROLLER(
                        key=f"audio_controller_{track_id}",
                        data={
                            "paused": paused,
                            "trackId": str(track_id),
                            "title": str(track.get("title") or ""),
                            "artist": str(track.get("artist") or ""),
                            "album": str(track.get("album") or ""),
                            "hasPrevious": current_index > 0,
                            "hasNext": current_index < len(queue_ids) - 1,
                            "reset": bool(st.session_state.get("audio_reset", False)),
                            "token": st.session_state.get("audio_command_nonce", 0),
                        },
                        default={"action": None},
                        on_action_change=lambda: None,
                    )
                    st.session_state.audio_reset = False
                    media_action = getattr(controller_result, "action", None)
                    if hasattr(media_action, "get") and media_action.get("token"):
                        action_token = str(media_action["token"])
                        if st.session_state.get("media_action_token") != action_token:
                            st.session_state.media_action_token = action_token
                            action = media_action.get("action")
                            if action == "previous" and current_index > 0:
                                skip_queue_track(queue_ids, current_index, -1)
                            elif action == "next" and current_index < len(queue_ids) - 1:
                                skip_queue_track(queue_ids, current_index, 1)
                            elif action == "play":
                                st.session_state.playback_paused = False
                            elif action == "pause":
                                st.session_state.playback_paused = True
                            else:
                                return
                            st.session_state.audio_command_nonce = st.session_state.get("audio_command_nonce", 0) + 1
                            st.rerun()
            except Exception as exc:
                st.error(f"Não foi possível tocar esta faixa: {exc}")


inject_theme()
connection = get_connection()
authenticator, username, display_name = require_login(connection)
store = GoogleSheetsStore(connection, owner_id=username)
load_library(store)
home_page = st.Page(
    render_home,
    title="Início",
    icon=":material/home:",
    default=True,
)
music_page = st.Page(
    lambda: render_music(store),
    title="Músicas",
    icon=":material/library_music:",
    url_path="musicas",
)
settings_page = st.Page(
    lambda: render_settings(store, authenticator),
    title="Configurações",
    icon=":material/settings:",
    url_path="configuracoes",
)
navigation = st.navigation(
    [home_page, music_page, settings_page], position="hidden"
)
sidebar_header(display_name)
sidebar_navigation(
    [
        (home_page, "Início", ":material/home:"),
        (music_page, "Músicas", ":material/library_music:"),
        (settings_page, "Configurações", ":material/settings:"),
    ]
)
sidebar_footer(store)
navigation.run()
render_player()

