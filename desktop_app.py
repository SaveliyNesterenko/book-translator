from __future__ import annotations

import socket
import threading
import time
import urllib.request
from pathlib import Path

import uvicorn
import webview

from web_app import app, manager
from workspace import project_lock, BusyError


HOST = "127.0.0.1"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


def main() -> None:
    # WebView2 uses a native Save As dialog for enabled downloads.
    webview.settings['ALLOW_DOWNLOADS'] = True
    port = free_port()
    url = f"http://{HOST}:{port}"
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True, name="book-translator-ui")
    thread.start()
    for _ in range(80):
        try:
            urllib.request.urlopen(url, timeout=0.5).close()
            break
        except Exception:
            time.sleep(0.1)
    else:
        raise RuntimeError("Не удалось запустить интерфейс")

    window = webview.create_window("Book Translator", url, width=1440, height=900, min_size=(880, 620), confirm_close=True)
    def closing():
        name = manager._active_project
        if name and manager.runtime_for(name)['active']:
            if window.create_confirmation_dialog('Перевод выполняется', 'Запросить паузу? Дождитесь сохранения текущего результата, затем закройте окно ещё раз.'):
                manager.request_stop(name)
            return False
        return True
    window.events.closing += closing
    webview.start(localization={'global.quitConfirmation':'Закрыть переводчик? Убедитесь, что ручные правки сохранены. Готовые версии останутся в библиотеке.'})
    manager.shutdown()
    server.should_exit = True
    thread.join(timeout=5)


if __name__ == "__main__":
    try:
        with project_lock(Path(__file__).with_name('.application')):
            main()
    except BusyError:
        print('Переводчик уже открыт. Перейдите к существующему окну.')
