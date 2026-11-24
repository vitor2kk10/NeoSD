"""Servidor HTTP mínimo (só 127.0.0.1) para as imagens grandes do modo console.

Por que: devolver um JPEG de 1920 px como data URI pela ponte do pywebview manda ~1 MB de texto a cada
troca de jogo, e no WebView2 isso pode falhar em silêncio. Aqui o JavaScript recebe só um link curto
(http://127.0.0.1:PORTA/TOKEN/bg/arquivo.jpg) e o navegador baixa e guarda a imagem em cache sozinho.

Só serve arquivos de pastas registradas, por nome simples (sem subpastas, sem '..'), e o caminho leva um
token aleatório: outro programa da máquina não consegue listar nada.
"""
import logging
import mimetypes
import os
import secrets
import threading
from urllib.parse import quote, unquote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("neosd.media")


class MediaServer:
    def __init__(self):
        self.token = secrets.token_hex(8)
        self.roots = {}
        self.httpd = None
        self.base = ""

    def add_root(self, name, folder):
        self.roots[name] = str(folder)

    def url(self, root, filename):
        """Link para um arquivo de uma pasta registrada, ou '' se o servidor não estiver no ar."""
        if not self.base:
            return ""
        return f"{self.base}/{root}/{quote(filename)}"

    def start(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # sem barulho no console
                pass

            def do_GET(self):
                parts = self.path.split("?", 1)[0].strip("/").split("/")
                if len(parts) != 3 or parts[0] != server.token or parts[1] not in server.roots:
                    return self.send_error(404)
                name = unquote(parts[2])
                if not name or name != os.path.basename(name) or name.startswith(".") or "\\" in name:
                    return self.send_error(404)
                path = os.path.join(server.roots[parts[1]], name)
                if not os.path.isfile(path):
                    return self.send_error(404)
                try:
                    with open(path, "rb") as f:
                        data = f.read()
                except OSError:
                    return self.send_error(404)
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=31536000, immutable")  # nome muda se o conteúdo mudar
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)

        try:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            self.httpd.daemon_threads = True
            self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}/{self.token}"
            threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        except OSError:
            log.exception("servidor de imagens não iniciou; o fundo volta a usar data URI")
            self.httpd, self.base = None, ""
        return self
