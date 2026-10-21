"""Shared credential storage for Yet Another Music Downloader."""

from dataclasses import dataclass, field
from pathlib import Path
import getpass
import json
import os
import re
import stat
import sys
import tempfile


class SettingsError(Exception):
    """A safe settings error that never includes credential values."""


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(default="", repr=False)
    chat_id: str = ""


def config_path():
    if sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "yamd" / "config.json"


def read_saved():
    path = config_path()
    try:
        # Refuse a symlink rather than reading an unrelated file as credentials.
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 16_384:
                raise SettingsError("Некорректный файл настроек. Открой «Настройки» для перезаписи.")
            data = json.load(stream)
        if not isinstance(data, dict) or any(not isinstance(data.get(key), str) for key in ("bot_token", "chat_id")):
            raise ValueError
        return Settings(data["bot_token"].strip(), data["chat_id"].strip())
    except FileNotFoundError:
        return Settings()
    except (OSError, ValueError, UnicodeError):
        raise SettingsError("Не удалось прочитать настройки. Проверь доступ к config.json или перезапиши его через «Настройки».") from None


def legacy_settings():
    if getattr(sys, "frozen", False):
        return Settings()
    try:
        from dotenv import dotenv_values
    except ImportError:
        raise SettingsError("Установи зависимости из requirements.txt или используй готовую сборку yamd.") from None

    try:
        values = dotenv_values(Path(__file__).resolve().parent / ".env", interpolate=False)
    except (OSError, UnicodeError):
        raise SettingsError("Не удалось прочитать прежний .env. Проверь права доступа и кодировку.") from None
    return Settings(
        (values.get("TELEGRAM_BOT_TOKEN") or "").strip(),
        (values.get("TELEGRAM_CHAT_ID") or "").strip(),
    )


def effective_settings():
    saved = read_saved()
    legacy = legacy_settings() if not (saved.bot_token and saved.chat_id) else Settings()
    return Settings(
        os.environ.get("TELEGRAM_BOT_TOKEN", saved.bot_token or legacy.bot_token).strip(),
        os.environ.get("TELEGRAM_CHAT_ID", saved.chat_id or legacy.chat_id).strip(),
    )


def editable_settings():
    saved = read_saved()
    legacy = legacy_settings() if not (saved.bot_token and saved.chat_id) else Settings()
    # Editing the persistent values must not silently replace them with env overrides.
    return Settings(
        saved.bot_token or legacy.bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        saved.chat_id or legacy.chat_id or os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
    )


def environment_notice():
    if any(key in os.environ for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")):
        return "Переменные TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID из окружения имеют приоритет над сохранёнными настройками."
    return ""


def validate_settings(settings):
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", settings.bot_token):
        raise SettingsError("Укажи токен бота в формате BotFather: число:секретная_часть.")
    if not re.fullmatch(r"(?:-?[0-9]+|@[A-Za-z][A-Za-z0-9_]{4,31})", settings.chat_id):
        raise SettingsError("Укажи числовой ID канала или @username (5–32 символа).")


def save_settings(settings):
    validate_settings(settings)
    path = config_path()
    temporary = None
    try:
        if path.parent.is_symlink() or path.is_symlink():
            raise SettingsError("Каталог или файл настроек не должен быть символической ссылкой.")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            json.dump({"bot_token": settings.bot_token, "chat_id": settings.chat_id}, stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise SettingsError("Не удалось сохранить настройки. Проверь права доступа и свободное место.") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def configure_cli():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Для --configure нужен интерактивный терминал.", file=sys.stderr)
        return 2
    try:
        try:
            current = editable_settings()
        except SettingsError as error:
            print(str(error), file=sys.stderr)
            current = Settings()
        print("Yet Another Music Downloader · настройки Telegram")
        print("Пустой ввод сохраняет прежнее значение. Токен не отображается.")
        print("Конфиг хранится без шифрования, с доступом только владельцу (0600).")
        if notice := environment_notice():
            print(notice)
        # Never fall back to echoed stdin if getpass cannot disable terminal echo.
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            token = getpass.getpass("Токен бота: ").strip() or current.bot_token
        chat_id = input("ID канала / @username: ").strip() or current.chat_id
        save_settings(Settings(token, chat_id))
    except (EOFError, KeyboardInterrupt):
        print("\nНастройки не изменены.", file=sys.stderr)
        return 130
    except getpass.GetPassWarning:
        print("Не удалось скрыть ввод токена. Используй обычный терминал или настройки TUI.", file=sys.stderr)
        return 2
    except SettingsError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(f"Настройки сохранены: {config_path()}")
    return 0
