#!/usr/bin/env python3
"""Download audio from SoundCloud / YouTube and publish MP3 to Telegram."""

import argparse
from dataclasses import dataclass
import re
from pathlib import Path
import shutil
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlencode, urlparse

from yamd_config import SettingsError, configure_cli, effective_settings

VERSION = "1.0.0"
MAX_AUDIO_BYTES = 49_000_000


class TransferError(Exception):
    """A safe, user-facing error without credentials."""


@dataclass
class Track:
    url: str
    title: str = ""


def downloader_options():
    return {
        "quiet": True,
        "no_warnings": True,
        "logger": QuietLogger(),
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "ignoreerrors": False,
        "js_runtimes": {"deno": {}, "node": {}},
    }


class QuietLogger:
    # Extractor diagnostics can include private URLs; expose only safe errors below.
    def debug(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass


YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}


def media_url(value):
    message = "Ожидается HTTPS-ссылка на трек/плейлист SoundCloud или видео/плейлист YouTube"
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise ValueError
        if host == "soundcloud.com" or host.endswith(".soundcloud.com"):
            return value
        if host not in YOUTUBE_HOSTS:
            raise ValueError
        query = parse_qs(parsed.query)
        if parsed.path == "/playlist" and host not in {"youtu.be", "www.youtu.be"}:
            playlist = query.get("list", [""])[0]
            if not re.fullmatch(r"[A-Za-z0-9_-]+", playlist):
                raise ValueError
            return "https://www.youtube.com/playlist?" + urlencode({"list": playlist})
        if host in {"youtu.be", "www.youtu.be"}:
            video = parsed.path.removeprefix("/")
        elif parsed.path == "/watch":
            video = query.get("v", [""])[0]
        elif parsed.path.startswith("/shorts/"):
            video = parsed.path.removeprefix("/shorts/").rstrip("/")
        else:
            raise ValueError
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video):
            raise ValueError
        # Drop list/index from video URLs so a single video never expands a playlist.
        return "https://www.youtube.com/watch?" + urlencode({"v": video})
    except ValueError:
        raise argparse.ArgumentTypeError(message) from None


def source_name(url):
    return "YouTube" if (urlparse(url).hostname or "").lower() in YOUTUBE_HOSTS else "SoundCloud"


def require_youtube_runtime(url):
    if source_name(url) == "YouTube" and not (shutil.which("deno") or shutil.which("node")):
        raise TransferError("Для YouTube установи Deno >= 2.3 или Node.js >= 22 и зависимости из requirements.txt.")


def reject_live(info, *, incomplete):
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
        return "Прямые эфиры и ожидаемые премьеры не поддерживаются."


def telegram_call(session, token, method, **kwargs):
    import requests

    try:
        response = session.post(
            f"https://api.telegram.org/bot{token}/{method}",
            timeout=(20, 300),
            allow_redirects=False,
            **kwargs,
        )
    except requests.RequestException as error:
        # Never include exception text: requests embeds the bot token in URLs.
        if isinstance(error, requests.exceptions.ProxyError):
            reason = "Ошибка соединения с прокси. Проверь HTTP_PROXY / HTTPS_PROXY / ALL_PROXY."
        elif isinstance(error, requests.exceptions.SSLError):
            reason = "Ошибка SSL/TLS. Проверь сертификаты, системное время и настройки VPN/прокси."
        elif isinstance(error, requests.exceptions.ConnectTimeout):
            reason = "Таймаут подключения к api.telegram.org (20 секунд)."
        elif isinstance(error, requests.exceptions.ReadTimeout):
            reason = "Таймаут ожидания ответа Telegram (300 секунд)."
        elif isinstance(error, requests.exceptions.Timeout):
            reason = "Превышено время сетевого запроса."
        elif isinstance(error, requests.exceptions.ConnectionError):
            reason = "Ошибка подключения: возможен сбой DNS, отказ или разрыв соединения. Проверь сеть и VPN/прокси."
        else:
            reason = "Сбой HTTP-запроса к Telegram."
        # Do not retry uploads: Telegram may have accepted the audio already.
        outcome = (
            "Проверь канал перед повтором: файл мог быть опубликован."
            if method == "sendAudio"
            else "Сбой проверки настроек; на этом этапе файлы не отправлялись."
        )
        raise TransferError(f"Telegram {method}: {reason} {outcome}") from None
    try:
        result = response.json()
    except ValueError:
        raise TransferError(f"Telegram вернул некорректный ответ (HTTP {response.status_code}).") from None
    if not isinstance(result, dict) or not response.ok or not result.get("ok"):
        code = result.get("error_code", response.status_code) if isinstance(result, dict) else response.status_code
        hint = {
            400: "Проверь ID канала, формат файла и права бота.",
            401: "Проверь токен бота.",
            403: "Добавь бота в канал с правом публикации.",
            429: "Слишком много запросов. Повтори запуск позже, проверив уже отправленные треки.",
        }.get(code, "Проверь канал перед повторной отправкой.")
        raise TransferError(f"Ошибка Telegram {code}. {hint}")
    return result["result"]


def resolve_tracks(url, base_options, with_titles=False):
    import yt_dlp

    url = media_url(url)
    require_youtube_runtime(url)
    options = {
        **base_options, "extract_flat": "in_playlist",
        "noplaylist": source_name(url) == "YouTube" and urlparse(url).path != "/playlist",
        "match_filter": reject_live,
    }
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=False)
    if not info:
        raise TransferError("Источник не вернул данные или недоступен.")
    if "entries" not in info:
        title = str(info.get("track") or info.get("title") or source_name(url))[:128]
        return [Track(url, title)] if with_titles else [url]
    tracks = []
    for entry in info["entries"]:
        if not entry:
            raise TransferError("Не удалось прочитать один из элементов плейлиста.")
        track_url = entry.get("webpage_url") or entry.get("url")
        if not track_url:
            raise TransferError("Не удалось получить ссылку на трек.")
        # Flat YouTube entries may contain only a video ID instead of a URL.
        if source_name(url) == "YouTube" and re.fullmatch(r"[A-Za-z0-9_-]{11}", track_url):
            track_url = "https://www.youtube.com/watch?" + urlencode({"v": track_url})
        track_url = media_url(track_url)
        title = str(entry.get("track") or entry.get("title") or source_name(track_url))[:128]
        tracks.append(Track(track_url, title) if with_titles else track_url)
    if not tracks:
        raise TransferError("Плейлист пуст.")
    return tracks


def build_caption(performer, title, url, note=""):
    caption = f"{performer} — {title}"
    if note:
        caption += f"\n\n{note}"
    # Telegram counts UTF-16 code units, not Python Unicode code points.
    if len(caption.encode("utf-16-le")) // 2 > 1024:
        raise TransferError("Подпись с припиской превышает лимит Telegram (1024 символа). Сократи текст.")
    return caption


def audio_filename(title):
    # Use a portable filename; the local temporary path stays fixed and private.
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", title).strip(" .")
    stem = stem.encode("utf-8")[:180].decode("utf-8", errors="ignore").rstrip(" .")
    if not stem or stem.upper().split(".")[0] in {
        "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        stem = "audio"
    return f"{stem}.mp3"


def transfer_track(url, base_options, session, token, chat_id, note="", report=print, title_override=""):
    import yt_dlp

    url = media_url(url)
    require_youtube_runtime(url)
    with tempfile.TemporaryDirectory(prefix="sc-to-tg-") as directory:
        report("Скачивание аудио…")
        converting = False

        def postprocessor_status(status):
            nonlocal converting
            if not converting:
                converting = True
                report("Конвертация в MP3…")

        options = {
            **base_options,
            "postprocessor_hooks": [postprocessor_status],
            "noplaylist": True,
            "format": "bestaudio",
            "match_filter": reject_live,
            "outtmpl": str(Path(directory) / "audio.%(ext)s"),
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        }
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=True)
        audio = Path(directory) / "audio.mp3"
        if not info or not audio.is_file():
            raise TransferError("Не удалось подготовить MP3.")
        size = audio.stat().st_size
        if size == 0 or size > MAX_AUDIO_BYTES:
            raise TransferError(f"Размер MP3 {size / 1_000_000:.1f} МБ: допустимо от 1 байта до 49 МБ.")
        title = (title_override or str(info.get("track") or info.get("title") or source_name(url)))[:128]
        performer = str(info.get("artist") or info.get("uploader") or info.get("channel") or source_name(url))[:128]
        data = {
            "chat_id": chat_id,
            "title": title,
            "performer": performer,
            "caption": build_caption(performer, title, url, note),
        }
        duration = info.get("duration")
        if isinstance(duration, (int, float)) and duration > 0:
            data["duration"] = int(duration)
        report(f"Отправка: {performer} — {title}…")
        with audio.open("rb") as stream:
            telegram_call(
                session, token, "sendAudio", data=data,
                files={"audio": (audio_filename(title), stream, "audio/mpeg")},
            )
        report(f"Отправлено: {performer} — {title}")


def run_transfer(urls, note="", report=print, should_stop=lambda: False, prepared_tracks=None):
    try:
        import requests
        import yt_dlp
    except ImportError:
        report("Установи зависимости: python3 -m pip install -r requirements.txt")
        return 2

    try:
        settings = effective_settings()
    except SettingsError as error:
        report(str(error))
        return 2
    token, chat_id = settings.bot_token, settings.chat_id
    if not token or not chat_id:
        report("Открой «Настройки» в TUI или запусти yamd --configure, чтобы задать токен бота и канал.")
        return 2
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        report("Установи FFmpeg (включая ffprobe), например: brew install ffmpeg")
        return 2
    base_options = downloader_options()
    sent, failed = 0, 0
    last_upload = 0.0
    with requests.Session() as session:
        report("Проверка канала и прав бота…")
        try:
            bot = telegram_call(session, token, "getMe")
            chat = telegram_call(session, token, "getChat", data={"chat_id": chat_id})
            member = telegram_call(session, token, "getChatMember", data={"chat_id": chat_id, "user_id": bot["id"]})
            if chat.get("type") != "channel":
                raise TransferError("Указанный chat_id не является каналом.")
            if member.get("status") != "creator" and not (
                member.get("status") == "administrator" and member.get("can_post_messages")
            ):
                raise TransferError("Боту нужны права администратора канала с разрешением публикации.")
        except TransferError as error:
            report(str(error))
            return 1
        sources = [None] if prepared_tracks is not None else urls
        for source_number, source in enumerate(sources, 1):
            if should_stop():
                break
            if prepared_tracks is None:
                report(f"Источник {source_number}: чтение трека/плейлиста…")
            try:
                tracks = prepared_tracks if prepared_tracks is not None else resolve_tracks(source, base_options)
            except TransferError as error:
                report(f"Источник {source_number}: {error}")
                failed += 1
                continue
            except (yt_dlp.utils.DownloadError, ValueError, argparse.ArgumentTypeError):
                report(f"Источник {source_number}: не удалось прочитать аудио/плейлист. Проверь доступность и обнови yt-dlp.")
                failed += 1
                continue
            for number, track in enumerate(tracks, 1):
                if should_stop():
                    break
                report(f"Источник {source_number}, трек {number}/{len(tracks)}")
                try:
                    # Space channel posts to avoid Telegram's per-chat rate limit.
                    time.sleep(max(0, 3.1 - (time.monotonic() - last_upload)))
                    last_upload = time.monotonic()
                    if should_stop():
                        break
                    url = track.url if isinstance(track, Track) else track
                    title = track.title if isinstance(track, Track) else ""
                    transfer_track(url, base_options, session, token, chat_id, note, report, title)
                    sent += 1
                except (TransferError, argparse.ArgumentTypeError) as error:
                    report(f"Ошибка: {error}")
                    failed += 1
                except yt_dlp.utils.DownloadError:
                    report("Ошибка скачивания/конвертации. Проверь доступность аудио и обнови yt-dlp[default]. Для YouTube нужны Deno >= 2.3 или Node.js >= 22.")
                    failed += 1
                except OSError:
                    report("Ошибка работы с локальными файлами или FFmpeg.")
                    failed += 1
    if should_stop():
        report("Очередь остановлена. Проверь канал перед повторным запуском.")
    report(f"Итог: отправлено {sent}, ошибок {failed}.")
    return 130 if should_stop() else (1 if failed else 0)


def main():
    parser = argparse.ArgumentParser(
        description="Yet Another Music Downloader: SoundCloud / YouTube → MP3 → Telegram.",
        epilog="Без аргументов открывается TUI. Настройка Telegram: --configure. Требуется FFmpeg.",
    )
    parser.add_argument("--version", action="version", version=f"Yet Another Music Downloader {VERSION}")
    parser.add_argument("urls", nargs="*", type=media_url, help="HTTPS-ссылки SoundCloud / YouTube: треки, видео или плейлисты")
    parser.add_argument("--note", default="", help="Приписка к каждому аудиосообщению (по умолчанию пустая)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--tui", action="store_true", help="Открыть терминальный интерфейс")
    mode.add_argument("--configure", action="store_true", help="Настроить токен Telegram и канал (скрытый ввод токена)")
    args = parser.parse_args()
    if args.configure:
        if args.urls or args.note:
            parser.error("Не совмещай --configure со ссылками или --note.")
        return configure_cli()
    if args.tui or not args.urls:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            parser.error("Для TUI нужен интерактивный терминал. Передай ссылки для обычного CLI.")
        try:
            from yamd_tui import YamdApp
        except ImportError:
            parser.exit(2, "Установи зависимости: python3 -m pip install -r requirements.txt\n")
        return YamdApp(args.urls, args.note).run()
    return run_transfer(args.urls, args.note)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nОстановлено. Проверь канал перед повторным запуском.", file=sys.stderr)
        sys.exit(130)
