"""Interactive terminal frontend for SoundCloud / YouTube audio transfers."""

import argparse
from threading import Event

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, RichLog, Static, TextArea

from yamd import Track, TransferError, audio_filename, downloader_options, media_url, resolve_tracks, run_transfer
from yamd_config import Settings, SettingsError, config_path, editable_settings, environment_notice, save_settings


class SettingsScreen(ModalScreen[bool]):
    DEFAULT_CSS = """
    SettingsScreen { align: center middle; background: $background 70%; }
    SettingsScreen > VerticalScroll {
        width: 72; height: auto; max-height: 90%; padding: 1 2;
        border: round $primary; background: $surface;
    }
    SettingsScreen Horizontal { height: auto; margin-top: 1; }
    SettingsScreen #settings-message { height: auto; margin-bottom: 1; }
    """
    BINDINGS = [("escape", "cancel", "Отмена")]

    def __init__(self):
        super().__init__()
        self.current = Settings()

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("НАСТРОЙКИ TELEGRAM")
            yield Label("Токен бота · скрытый ввод, пусто — оставить прежний")
            yield Input(password=True, id="bot-token")
            yield Label("ID канала / @username")
            yield Input(id="chat-id")
            yield Static(
                f"{config_path()}\nФайл без шифрования, доступ только владельцу (0600).\n"
                + environment_notice(), markup=False,
            )
            yield Static("", id="settings-message", markup=False)
            with Horizontal():
                yield Button("Сохранить", id="save-settings", variant="primary")
                yield Button("Отмена", id="cancel-settings")

    def on_mount(self) -> None:
        try:
            self.current = editable_settings()
        except SettingsError as error:
            self.query_one("#settings-message", Static).update(str(error))
        self.query_one("#chat-id", Input).value = self.current.chat_id
        if self.current.bot_token:
            self.query_one("#bot-token", Input).placeholder = "Токен уже задан · введи новый для замены"
        self.query_one("#bot-token", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "cancel-settings":
            self.dismiss(False)
        elif event.button.id == "save-settings":
            self.save()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if event.input.id == "bot-token":
            self.query_one("#chat-id", Input).focus()
        elif event.input.id == "chat-id":
            self.save()

    def save(self) -> None:
        token = self.query_one("#bot-token", Input).value.strip() or self.current.bot_token
        chat_id = self.query_one("#chat-id", Input).value.strip() or self.current.chat_id
        try:
            save_settings(Settings(token, chat_id))
        except SettingsError as error:
            self.query_one("#settings-message", Static).update(str(error))
            return
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class StopConfirmation(ModalScreen[bool]):
    DEFAULT_CSS = """
    StopConfirmation { align: center middle; background: $background 70%; }
    StopConfirmation > VerticalScroll {
        width: 64; height: auto; max-height: 90%; padding: 1 2;
        border: round $warning; background: $surface;
    }
    StopConfirmation Horizontal { height: auto; margin-top: 1; }
    StopConfirmation Button { margin-right: 1; }
    """
    BINDINGS = [("escape", "back", "Назад")]

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("Остановить очередь и выйти?")
            yield Static(
                "Дождёмся завершения текущей операции. Текущий трек может быть опубликован. "
                "Следующие треки не начнутся. Перед повторным запуском проверь канал."
            )
            with Horizontal():
                yield Button("Продолжить", id="back", variant="primary")
                yield Button("Остановить и выйти", id="confirm", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_back(self) -> None:
        self.dismiss(False)


class YamdApp(App[int]):
    TITLE = "Yet Another Music Downloader"
    SUB_TITLE = "Ссылки → названия → отправка"
    CSS = """
    Screen { background: $background; }
    #form { height: 1fr; padding: 1 2; }
    Label { margin-bottom: 1; color: $text-muted; }
    TextArea, Input { border: round $primary; margin-bottom: 1; }
    #urls { height: 5; min-height: 3; }
    #note { height: 4; min-height: 3; }
    .actions { height: auto; margin-bottom: 1; }
    #links-heading { height: 3; }
    #links-heading Label { width: 1fr; content-align: left middle; }
    #clear-links { min-width: 12; width: 12; margin-right: 0; }
    Button { margin-right: 2; }
    DataTable { height: 8; margin-bottom: 1; }
    #status { padding: 0 2; height: auto; color: $accent; }
    RichLog { height: 6; margin: 0 2 1 2; border: round $panel; }
    """
    BINDINGS = [
        Binding("ctrl+q", "request_quit", "Выход", priority=True),
        Binding("ctrl+c", "request_quit", "Выход", show=False, priority=True),
        Binding("ctrl+enter", "send", "Отправить", priority=True),
    ]

    def __init__(self, urls=(), note=""):
        super().__init__()
        self.initial_urls = "\n".join(urls)
        self.initial_note = note
        self.busy = False
        self.stop_requested = Event()
        self.closing = False
        self.last_result = 0
        self.tracks: list[Track] = []
        self.original_titles: list[str] = []
        self.selected_index: int | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="form"):
            with Horizontal(id="links-heading"):
                yield Label("ССЫЛКИ · SoundCloud / YouTube")
                yield Button("Очистить", id="clear-links")
            yield TextArea(self.initial_urls, id="urls")
            yield Label("ПРИПИСКА · необязательно, добавится к каждому аудио")
            yield TextArea(self.initial_note, id="note")
            with Horizontal(classes="actions"):
                yield Button("Загрузить список", id="prepare", variant="primary")
                yield Button("Отправить", id="send", variant="success", disabled=True)
            with Horizontal(classes="actions"):
                yield Button("Настройки", id="settings")
                yield Button("Выход", id="quit")
            yield Label("ТРЕКИ · выбери строку, чтобы изменить название")
            yield DataTable(id="tracks", cursor_type="row", zebra_stripes=True)
            yield Input(placeholder="Название трека и MP3 · пустое поле вернёт оригинал", id="title", max_length=128, disabled=True)
            with Horizontal(classes="actions"):
                yield Button("Сохранить название", id="save", disabled=True)
        yield Static("Вставь ссылки и загрузи список · Telegram: кнопка «Настройки»", id="status", markup=False)
        yield RichLog(id="events", wrap=True, markup=False, highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#tracks", DataTable)
        table.add_column("№", key="number", width=4)
        table.add_column("Название / имя MP3", key="title")
        table.add_column("Ссылка", key="url")
        self.query_one("#urls", TextArea).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "settings": self.action_settings,
            "clear-links": self.clear_links,
            "prepare": self.prepare_queue,
            "send": self.action_send,
            "save": lambda: self.save_title(announce=True),
            "quit": self.action_request_quit,
        }
        if action := actions.get(event.button.id):
            action()

    def show_event(self, message: str) -> None:
        self.query_one("#status", Static).update(message)
        self.query_one("#events", RichLog).write(message)

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        for selector in ("#urls", "#note", "#prepare", "#tracks", "#clear-links", "#settings"):
            self.query_one(selector).disabled = busy
        self.query_one("#send", Button).disabled = busy or not self.tracks
        for selector in ("#title", "#save"):
            self.query_one(selector).disabled = busy or self.selected_index is None

    def action_settings(self) -> None:
        if not self.busy and not isinstance(self.screen, SettingsScreen):
            self.push_screen(SettingsScreen(), self.settings_saved)

    def settings_saved(self, saved: bool) -> None:
        if saved:
            message = "Настройки сохранены и применятся к следующей отправке."
            if notice := environment_notice():
                message += f" {notice}"
            self.show_event(message)

    def reset_queue(self) -> None:
        self.tracks = []
        self.original_titles = []
        self.selected_index = None
        self.query_one("#tracks", DataTable).clear()
        self.query_one("#title", Input).value = ""
        self.set_busy(False)

    def clear_links(self) -> None:
        if self.busy:
            return
        self.query_one("#urls", TextArea).text = ""
        self.reset_queue()
        self.query_one("#urls", TextArea).focus()
        self.show_event("Ссылки и список треков очищены. Приписка сохранена.")

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "urls" and self.tracks and not self.busy:
            self.reset_queue()
            self.show_event("Ссылки изменились. Загрузи список заново.")

    def prepare_queue(self) -> None:
        if self.busy:
            return
        raw_urls = self.query_one("#urls", TextArea).text.split()
        if not raw_urls:
            self.show_event("Добавь хотя бы одну HTTPS-ссылку SoundCloud или YouTube.")
            return
        try:
            urls = [media_url(value) for value in raw_urls]
        except (argparse.ArgumentTypeError, ValueError):
            self.show_event("Проверь ссылки: нужны HTTPS-ссылки SoundCloud или YouTube (видео/плейлисты).")
            return
        self.stop_requested.clear()
        self.set_busy(True)
        self.query_one("#events", RichLog).clear()
        self.show_event("Чтение названий треков · публикации пока нет…")
        self.load_tracks(urls)

    @work(thread=True, exclusive=True, group="prepare")
    def load_tracks(self, urls: list[str]) -> None:
        tracks = []
        failed = False
        try:
            options = downloader_options()
            for number, url in enumerate(urls, 1):
                if self.stop_requested.is_set():
                    break
                self.call_from_thread(self.show_event, f"Источник {number}/{len(urls)}: чтение треков…")
                tracks.extend(resolve_tracks(url, options, with_titles=True))
        except TransferError as error:
            self.call_from_thread(self.show_event, str(error))
            failed = True
        except Exception:
            # Do not expose extractor errors: they may contain private URLs.
            failed = True
        self.call_from_thread(self.finish_preparing, tracks, failed)

    def finish_preparing(self, tracks: list[Track], failed: bool) -> None:
        if self.closing:
            self.exit(130)
            return
        self.selected_index = None
        self.tracks = [] if failed else tracks
        self.original_titles = [track.title for track in self.tracks]
        table = self.query_one("#tracks", DataTable)
        table.clear()
        for index, track in enumerate(self.tracks):
            table.add_row(str(index + 1), Text(track.title), Text(track.url), key=str(index))
        self.query_one("#title", Input).value = ""
        self.set_busy(False)
        self.last_result = 1 if failed else 0
        if failed:
            self.show_event("Не удалось прочитать список. Проверь ссылки, сеть, yt-dlp[default] и JS-runtime для YouTube. Ничего не отправлено.")
        elif self.tracks:
            self.select_track(0)
            self.show_event(f"Треков: {len(self.tracks)}. Измени названия при необходимости и нажми «Отправить».")
            table.focus()
        else:
            self.show_event("Треков не найдено. Ничего не отправлено.")

    def select_track(self, index: int) -> None:
        self.save_title()
        self.selected_index = index
        self.query_one("#title", Input).value = self.tracks[index].title
        self.query_one("#title", Input).disabled = False
        self.query_one("#save", Button).disabled = False

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not self.busy and event.cursor_row < len(self.tracks):
            self.select_track(event.cursor_row)

    def save_title(self, announce=False) -> None:
        index = self.selected_index
        if self.busy or index is None or index >= len(self.tracks):
            return
        title = self.query_one("#title", Input).value.strip() or self.original_titles[index]
        self.tracks[index].title = title
        self.query_one("#tracks", DataTable).update_cell(str(index), "title", Text(title))
        if announce:
            self.query_one("#title", Input).value = title
            self.show_event(f"Название сохранено · файл: {audio_filename(title)}")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "title":
            self.save_title(announce=True)

    def action_send(self) -> None:
        if self.busy:
            return
        if not self.tracks:
            self.show_event("Сначала загрузи список треков.")
            return
        note = self.query_one("#note", TextArea).text
        if len(note.encode("utf-16-le")) // 2 > 1024:
            self.show_event("Приписка слишком длинная: вместе с исполнителем и названием доступно 1024 символа.")
            return
        self.save_title()
        self.stop_requested.clear()
        self.set_busy(True)
        self.show_event("Начинаем отправку…")
        # Freeze the edited queue for the worker; widgets are owned by the UI thread.
        self.transfer([Track(track.url, track.title) for track in self.tracks], note)

    @work(thread=True, exclusive=True, group="transfer")
    def transfer(self, tracks: list[Track], note: str) -> None:
        try:
            result = run_transfer(
                [], note,
                report=lambda message: self.call_from_thread(self.show_event, message),
                should_stop=self.stop_requested.is_set,
                prepared_tracks=tracks,
            )
        except Exception:
            self.call_from_thread(
                self.show_event,
                "Непредвиденная ошибка. Проверь канал перед повторной отправкой.",
            )
            result = 1
        self.call_from_thread(self.finish_transfer, result)

    def finish_transfer(self, result: int) -> None:
        self.last_result = result
        if self.closing:
            self.exit(130)
            return
        self.set_busy(False)
        self.query_one("#send", Button).focus()

    def action_request_quit(self) -> None:
        if not self.busy:
            self.exit(self.last_result)
        elif not self.closing and not isinstance(self.screen, StopConfirmation):
            self.push_screen(StopConfirmation(), self.confirm_quit)

    def confirm_quit(self, confirmed: bool) -> None:
        if not confirmed:
            return
        if not self.busy:
            self.exit(self.last_result)
            return
        self.closing = True
        self.stop_requested.set()
        self.query_one("#quit", Button).disabled = True
        self.show_event("Остановка после текущей операции… Ожидаем безопасного завершения.")
