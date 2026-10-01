"""Minimális SMTP-álszerver a próbákhoz (az smtpd modul a Python 3.12-ben megszűnt): EHLO, AUTH PLAIN/LOGIN, MAIL, RCPT, DATA."""
import base64
import email
import email.policy
import socketserver
import threading


class MockSMTP:
    def __init__(self, user="hello@pacsit.hu", password="smtp-jelszo-123"):
        self.user, self.password = user, password
        self.messages = []          # EmailMessage-ek
        self.auth_failures = 0
        self.server = None
        self.port = 0

    def start(self):
        outer = self

        class H(socketserver.StreamRequestHandler):
            def send(self, line):
                self.wfile.write((line + "\r\n").encode("utf-8"))
                self.wfile.flush()

            def handle(self):
                self.send("220 mock ESMTP")
                authed, mail_from, rcpts = False, None, []
                while True:
                    raw = self.rfile.readline()
                    if not raw:
                        return
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    cmd = line.upper()
                    if cmd.startswith("EHLO") or cmd.startswith("HELO"):
                        self.wfile.write(b"250-mock\r\n250 AUTH PLAIN LOGIN\r\n")
                        self.wfile.flush()
                    elif cmd.startswith("AUTH PLAIN"):
                        parts = base64.b64decode(line.split(" ", 2)[2]).split(b"\0")
                        ok = len(parts) == 3 and parts[1].decode() == outer.user and parts[2].decode() == outer.password
                        if ok:
                            authed = True
                            self.send("235 ok")
                        else:
                            outer.auth_failures += 1
                            self.send("535 5.7.8 hibás belépés")
                    elif cmd.startswith("AUTH LOGIN"):
                        self.send("334 VXNlcm5hbWU6")
                        u = base64.b64decode(self.rfile.readline().strip()).decode()
                        self.send("334 UGFzc3dvcmQ6")
                        p = base64.b64decode(self.rfile.readline().strip()).decode()
                        if u == outer.user and p == outer.password:
                            authed = True
                            self.send("235 ok")
                        else:
                            outer.auth_failures += 1
                            self.send("535 5.7.8 hibás belépés")
                    elif cmd.startswith("MAIL FROM"):
                        if not authed:
                            self.send("530 5.7.0 belépés kell")
                            continue
                        mail_from = line[10:].strip()
                        self.send("250 ok")
                    elif cmd.startswith("RCPT TO"):
                        rcpts.append(line[8:].strip())
                        self.send("250 ok")
                    elif cmd == "DATA":
                        self.send("354 mehet")
                        buf = []
                        while True:
                            l = self.rfile.readline()
                            if l.rstrip(b"\r\n") == b".":
                                break
                            buf.append(l[1:] if l.startswith(b"..") else l)
                        msg = email.message_from_bytes(b"".join(buf), policy=email.policy.default)
                        msg["X-Envelope-To"] = ", ".join(rcpts)
                        outer.messages.append(msg)
                        self.send("250 ok")
                    elif cmd == "QUIT":
                        self.send("221 viszlát")
                        return
                    elif cmd == "RSET":
                        self.send("250 ok")
                    else:
                        self.send("502 nem támogatott")

        class S(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = S(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.05), daemon=True).start()
        return self

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    def text_of(self, msg):
        body = msg.get_body(preferencelist=("plain",))
        return body.get_content() if body else ""

    def html_of(self, msg):
        body = msg.get_body(preferencelist=("html",))
        return body.get_content() if body else ""
