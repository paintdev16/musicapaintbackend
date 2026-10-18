import sqlite3
import re
import unicodedata
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent / "music.db"


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db():
    conn = get_db()
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT NOT NULL,
                title TEXT NOT NULL,
                artist TEXT NOT NULL,
                genre TEXT,
                duration INTEGER,
                thumbnail TEXT,
                played_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_history_video_id ON history(video_id);
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS favorites (
                video_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                artist TEXT NOT NULL,
                genre TEXT,
                duration INTEGER,
                thumbnail TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS playlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS playlist_songs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                playlist_id INTEGER NOT NULL,
                video_id TEXT NOT NULL,
                title TEXT NOT NULL,
                artist TEXT NOT NULL,
                genre TEXT,
                duration INTEGER,
                thumbnail TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE CASCADE,
                UNIQUE(playlist_id, video_id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_playlist_songs ON playlist_songs(playlist_id);
            """
        )
        for table in ("history", "favorites", "playlist_songs"):
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if "genre" not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN genre TEXT")
    conn.close()


# Historial
def add_to_history(song: dict[str, Any]) -> None:
    video_id = song.get("id") or song.get("video_id")
    if not video_id:
        return
    title = song.get("title") or "Sin título"
    artist = song.get("artist") or "Desconocido"
    genre = song.get("genre")
    duration = song.get("duration")
    thumbnail = song.get("thumbnail") or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

    conn = get_db()
    with conn:
        conn.execute(
            """
            INSERT INTO history (video_id, title, artist, genre, duration, thumbnail, played_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (video_id, title, artist, genre, duration, thumbnail),
        )
        # Mantener un límite razonable en el historial (últimas 300 canciones)
        conn.execute(
            """
            DELETE FROM history WHERE id NOT IN (
                SELECT id FROM history ORDER BY played_at DESC LIMIT 300
            )
            """
        )
    conn.close()


def get_history(limit: int = 50) -> list[dict[str, Any]]:
    conn = get_db()
    # Leer todo el historial retenido permite llenar el límite con canciones únicas,
    # aunque haya muchas reproducciones repetidas entre las más recientes.
    rows = conn.execute(
        """
        SELECT id, video_id, title, artist, genre, duration, thumbnail, played_at
        FROM history
        ORDER BY played_at DESC, id DESC
        LIMIT 300
        """
    ).fetchall()
    conn.close()

    def title_tokens(title: str) -> set[str]:
        normalized = unicodedata.normalize("NFKD", title or "")
        normalized = "".join(char for char in normalized if not unicodedata.combining(char))
        normalized = re.sub(r"\[[^\]]*\]", " ", normalized)
        normalized = re.sub(r"\b(official|video|audio|lyrics?|letra|hd|4k|remastered|feat|ft)\b", " ", normalized, flags=re.IGNORECASE)
        return set(re.findall(r"[a-z0-9]+", normalized.casefold()))

    unique_rows = []
    seen_ids: set[str] = set()
    seen_titles: list[set[str]] = []
    for row in rows:
        video_id = row["video_id"]
        if video_id in seen_ids:
            continue
        tokens = title_tokens(row["title"])
        duplicate_title = False
        for previous in seen_titles:
            overlap = len(tokens & previous)
            smallest = min(len(tokens), len(previous))
            union = len(tokens | previous)
            if overlap >= 3 and (overlap / union >= 0.78 or overlap / smallest >= 0.88):
                duplicate_title = True
                break
        if duplicate_title:
            continue
        seen_ids.add(video_id)
        seen_titles.append(tokens)
        unique_rows.append(row)
        if len(unique_rows) >= limit:
            break

    return [
        {
            "history_id": r["id"],
            "id": r["video_id"],
            "title": r["title"],
            "artist": r["artist"],
            "genre": r["genre"],
            "duration": r["duration"],
            "thumbnail": r["thumbnail"],
            "played_at": r["played_at"],
        }
        for r in unique_rows
    ]


def clear_history() -> None:
    conn = get_db()
    with conn:
        conn.execute("DELETE FROM history")
    conn.close()


# Favoritos
def add_favorite(song: dict[str, Any]) -> None:
    video_id = song.get("id") or song.get("video_id")
    if not video_id:
        return
    title = song.get("title") or "Sin título"
    artist = song.get("artist") or "Desconocido"
    genre = song.get("genre")
    duration = song.get("duration")
    thumbnail = song.get("thumbnail") or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

    conn = get_db()
    with conn:
        conn.execute(
            """
            INSERT INTO favorites (video_id, title, artist, genre, duration, thumbnail, added_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(video_id) DO UPDATE SET
                title=excluded.title,
                artist=excluded.artist,
                genre=excluded.genre,
                duration=excluded.duration,
                thumbnail=excluded.thumbnail
            """,
            (video_id, title, artist, genre, duration, thumbnail),
        )
    conn.close()


def remove_favorite(video_id: str) -> None:
    conn = get_db()
    with conn:
        conn.execute("DELETE FROM favorites WHERE video_id = ?", (video_id,))
    conn.close()


def is_favorite(video_id: str) -> bool:
    conn = get_db()
    row = conn.execute("SELECT 1 FROM favorites WHERE video_id = ?", (video_id,)).fetchone()
    conn.close()
    return row is not None


def get_favorites(limit: int = 100) -> list[dict[str, Any]]:
    conn = get_db()
    rows = conn.execute(
        """
        SELECT video_id, title, artist, genre, duration, thumbnail, added_at
        FROM favorites
        ORDER BY added_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return [
        {
            "id": r["video_id"],
            "title": r["title"],
            "artist": r["artist"],
            "genre": r["genre"],
            "duration": r["duration"],
            "thumbnail": r["thumbnail"],
            "added_at": r["added_at"],
        }
        for r in rows
    ]


# Playlists
def create_playlist(name: str, description: str = "") -> dict[str, Any]:
    conn = get_db()
    with conn:
        cur = conn.execute(
            """
            INSERT INTO playlists (name, description, created_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            """,
            (name.strip(), description.strip()),
        )
        playlist_id = cur.lastrowid
    conn.close()
    return {"id": playlist_id, "name": name.strip(), "description": description.strip(), "songs_count": 0}


def get_playlists() -> list[dict[str, Any]]:
    conn = get_db()
    rows = conn.execute(
        """
        SELECT p.id, p.name, p.description, p.created_at, COUNT(s.id) as songs_count,
               (SELECT thumbnail FROM playlist_songs WHERE playlist_id = p.id ORDER BY added_at DESC LIMIT 1) as cover
        FROM playlists p
        LEFT JOIN playlist_songs s ON p.id = s.playlist_id
        GROUP BY p.id
        ORDER BY p.created_at DESC
        """
    ).fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "description": r["description"],
            "created_at": r["created_at"],
            "songs_count": r["songs_count"],
            "cover": r["cover"],
        }
        for r in rows
    ]


def get_playlist(playlist_id: int) -> dict[str, Any] | None:
    conn = get_db()
    playlist = conn.execute("SELECT id, name, description, created_at FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
    if not playlist:
        conn.close()
        return None

    songs = conn.execute(
        """
        SELECT video_id, title, artist, genre, duration, thumbnail, added_at
        FROM playlist_songs
        WHERE playlist_id = ?
        ORDER BY added_at ASC
        """,
        (playlist_id,),
    ).fetchall()
    conn.close()

    song_list = [
        {
            "id": s["video_id"],
            "title": s["title"],
            "artist": s["artist"],
            "genre": s["genre"],
            "duration": s["duration"],
            "thumbnail": s["thumbnail"],
            "added_at": s["added_at"],
        }
        for s in songs
    ]

    return {
        "id": playlist["id"],
        "name": playlist["name"],
        "description": playlist["description"],
        "created_at": playlist["created_at"],
        "songs_count": len(song_list),
        "songs": song_list,
    }


def delete_playlist(playlist_id: int) -> bool:
    conn = get_db()
    with conn:
        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
        deleted = cur.rowcount > 0
        if deleted:
            conn.execute("DELETE FROM playlist_songs WHERE playlist_id = ?", (playlist_id,))
    conn.close()
    return deleted


def add_song_to_playlist(playlist_id: int, song: dict[str, Any]) -> bool:
    video_id = song.get("id") or song.get("video_id")
    if not video_id:
        return False
    title = song.get("title") or "Sin título"
    artist = song.get("artist") or "Desconocido"
    genre = song.get("genre")
    duration = song.get("duration")
    thumbnail = song.get("thumbnail") or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

    conn = get_db()
    try:
        with conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO playlist_songs (playlist_id, video_id, title, artist, genre, duration, thumbnail, added_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (playlist_id, video_id, title, artist, genre, duration, thumbnail),
            )
        return True
    except Exception:
        return False
    finally:
        conn.close()


def remove_song_from_playlist(playlist_id: int, video_id: str) -> bool:
    conn = get_db()
    with conn:
        cur = conn.execute(
            "DELETE FROM playlist_songs WHERE playlist_id = ? AND video_id = ?",
            (playlist_id, video_id),
        )
        deleted = cur.rowcount > 0
    conn.close()
    return deleted
