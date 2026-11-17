# Wi-Fi setup from a phone: pibody.wifi_setup()
#
# The board opens its own Wi-Fi network ("<board name> setup", WPA2, a fresh
# 8-digit password) and shows a QR code. Scanning it with the phone camera
# joins that network; the phone then opens the setup page by itself (captive
# portal: every name the phone looks up points at the board). The page lists
# the networks the board can see; pick one, type its password, Connect. The
# board tries it, shows the result on its screen and saves it to /wifi.json
# on success, so WiFi().connect() needs no arguments from then on.
#
# Phones never share their saved passwords with a page or a device, so the
# password is typed once. The board's own network is WPA2, so what is typed
# is not sent in the clear.

import network, socket, select, time, os

PAGE_TIMEOUT = 300  # s with no page visits before setup gives up
_OTHER = "--other--"  # the "Other network..." choice


def _board_name():
    try:
        import bleterm
        name = bleterm.status().get("name")
        if name:
            return name
    except Exception:
        pass
    import machine, binascii
    return "PiBody-" + binascii.hexlify(machine.unique_id()).decode()[-4:].upper()


def _unquote(b):
    b = b.replace(b"+", b" ")
    out = bytearray()
    i = 0
    while i < len(b):
        c = b[i]
        if c == 37 and i + 2 < len(b):  # %XX
            try:
                out.append(int(b[i + 1:i + 3], 16))
                i += 3
                continue
            except ValueError:
                pass
        out.append(c)
        i += 1
    try:
        return out.decode()
    except UnicodeError:
        return ""


def _form(body):
    fields = {}
    for part in body.split(b"&"):
        k, _, v = part.partition(b"=")
        fields[_unquote(k)] = _unquote(v)
    return fields


def _html(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


_STYLE = """body{font-family:-apple-system,system-ui,sans-serif;margin:0;background:#f4f6f9;color:#1c1f26}
main{max-width:420px;margin:0 auto;padding:20px}h1{font-size:22px;margin:4px 0 16px}
label{display:block;font-size:14px;color:#596070;margin:14px 0 6px}
select,input{width:100%;box-sizing:border-box;font-size:17px;padding:12px;border:1px solid #c4cad4;border-radius:10px;background:#fff}
button{width:100%;margin-top:22px;font-size:17px;padding:14px;border:0;border-radius:10px;background:#0a70f5;color:#fff}
.msg{padding:12px;border-radius:10px;margin-bottom:8px}.err{background:#fde8e8;color:#a01c1c}.ok{background:#def5e5;color:#1d6b35}
.row{display:flex;gap:8px;align-items:center;margin-top:10px;font-size:15px}.row input{width:auto}"""


class _Setup:
    def __init__(self):
        self.sta = network.WLAN(network.STA_IF)
        self.ap = network.WLAN(network.AP_IF)
        self.name = _board_name()
        self.ssid = (self.name + " setup")[:32]
        self.key = "".join(str(b % 10) for b in os.urandom(8))
        self.ip = "192.168.4.1"
        self.networks = []
        self.msg = ""
        self.dns = self.web = None
        self.display = None

    # --- Wi-Fi --------------------------------------------------------------

    def scan(self):
        self.sta.active(True)
        try:
            found = self.sta.scan()
        except OSError:
            found = []
        best = {}
        for n in found:
            try:
                name = n[0].decode()
            except UnicodeError:
                continue
            if name and (name not in best or n[3] > best[name]):
                best[name] = n[3]
        self.networks = sorted(best, key=lambda k: -best[k])[:20]

    def start_ap(self):
        self.ap.active(False)
        self.ap.config(ssid=self.ssid, key=self.key)
        self.ap.active(True)
        t0 = time.ticks_ms()
        while time.ticks_diff(time.ticks_ms(), t0) < 5000:
            ip = self.ap.ifconfig()[0]
            if self.ap.active() and ip != "0.0.0.0":
                self.ip = ip
                return
            time.sleep_ms(100)

    def try_network(self, ssid, password):
        from .WiFi import WiFi, save_network
        w = WiFi()
        if w.wlan.isconnected():
            w.wlan.disconnect()
        result = w._join(ssid, password, 30)  # weak signal: joins can take 20 s
        save_network(ssid, password)
        return result

    # --- servers ------------------------------------------------------------

    def open_sockets(self):
        self.dns = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.dns.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.dns.bind(("0.0.0.0", 53))
        self.web = socket.socket()
        self.web.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.web.bind(("0.0.0.0", 80))
        self.web.listen(4)

    def close(self):
        for s in (self.dns, self.web):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        self.dns = self.web = None
        try:
            self.ap.active(False)
        except OSError:
            pass

    def answer_dns(self):
        """Every A lookup answers with the board's address (captive portal)."""
        try:
            data, addr = self.dns.recvfrom(512)
        except OSError:
            return
        if len(data) < 17:
            return
        i = 12
        while i < len(data) and data[i]:
            i += data[i] + 1
        i += 5
        if i > len(data):
            return
        qtype = data[i - 4] << 8 | data[i - 3]
        question = data[12:i]
        head = data[:2] + b"\x81\x80\x00\x01"
        if qtype == 1:
            ip = bytes(int(p) for p in self.ip.split("."))
            reply = (head + b"\x00\x01\x00\x00\x00\x00" + question +
                     b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04" + ip)
        else:
            reply = head + b"\x00\x00\x00\x00\x00\x00" + question
        try:
            self.dns.sendto(reply, addr)
        except OSError:
            pass

    def serve_http(self):
        """One request. Returns (ssid, password) when the form was sent."""
        try:
            conn, _ = self.web.accept()
        except OSError:
            return None
        found = None
        try:
            conn.settimeout(3)
            req = b""
            while b"\r\n\r\n" not in req and len(req) < 4096:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                req += chunk
            head, _, body = req.partition(b"\r\n\r\n")
            lines = head.split(b"\r\n")
            parts = lines[0].split(b" ")
            if len(parts) < 2:
                return None
            method, path = parts[0], parts[1].split(b"?")[0]
            length = 0
            for line in lines[1:]:
                if line.lower().startswith(b"content-length:"):
                    try:
                        length = min(int(line[15:].strip()), 2048)
                    except ValueError:
                        pass
            while len(body) < length:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                body += chunk
            if method == b"POST" and path == b"/save":
                found = self.handle_form(_form(body))
                if found:
                    self.send(conn, self.page_saved(found[0]))
                else:
                    self.send(conn, self.page_form())
            elif path in (b"/", b"/index.html"):
                self.send(conn, self.page_form())
            else:
                conn.sendall(("HTTP/1.1 302 Found\r\nLocation: http://%s/\r\n"
                              "Content-Length: 0\r\nConnection: close\r\n\r\n" % self.ip).encode())
        except OSError:
            pass
        finally:
            conn.close()
        return found

    def send(self, conn, html):
        body = html.encode()
        conn.sendall(("HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                      "Content-Length: %d\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n"
                      % len(body)).encode())
        conn.sendall(body)

    def handle_form(self, f):
        ssid = f.get("other", "").strip() if f.get("net") == _OTHER else f.get("net", "")
        password = f.get("password", "")
        if not ssid or len(ssid.encode()) > 32:
            self.msg = "Choose a network or type its name."
        elif password and not 8 <= len(password) <= 63:
            self.msg = "A Wi-Fi password has 8 to 63 characters."
        else:
            self.msg = ""
            return ssid, password
        return None

    # --- pages --------------------------------------------------------------

    def page(self, inner):
        return ('<!doctype html><html><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>%s Wi-Fi</title><style>%s</style></head><body><main>%s</main></body></html>'
                % (_html(self.name), _STYLE, inner))

    def page_form(self):
        opts = "".join('<option value="%s">%s</option>' % (_html(n), _html(n)) for n in self.networks)
        msg = '<div class="msg err">%s</div>' % _html(self.msg) if self.msg else ""
        return self.page(
            '<h1>Connect %s to Wi-Fi</h1>%s'
            '<form method="post" action="/save">'
            '<label for="net">Network</label>'
            '<select id="net" name="net" onchange="o.style.display=this.value==\'%s\'?\'block\':\'none\'">'
            '%s<option value="%s">Other network...</option></select>'
            '<input id="o" name="other" placeholder="Network name" style="display:%s;margin-top:8px">'
            '<label for="pw">Password</label>'
            '<input id="pw" name="password" type="password" autocomplete="current-password">'
            '<div class="row"><input id="sh" type="checkbox" onclick="pw.type=this.checked?\'text\':\'password\'">'
            '<label for="sh" style="margin:0">Show password</label></div>'
            '<button type="submit">Connect</button></form>'
            % (_html(self.name), msg, _OTHER, opts, _OTHER, "none" if self.networks else "block"))

    def page_saved(self, ssid):
        return self.page(
            '<h1>Connecting...</h1><div class="msg ok">%s is connecting to <b>%s</b>. '
            'Look at its screen for the result. If it fails, this page comes back '
            'so you can try again.</div>' % (_html(self.name), _html(ssid)))

    # --- screen -------------------------------------------------------------

    def screen(self, lines, show_qr=True, color=0x18E4):
        print("\n".join(lines))
        d = self.display
        if d is None:
            try:
                from DisplayBase import DisplayBase
                d = self.display = DisplayBase()
            except Exception:
                return
        try:
            from .qr import encode, wifi_text, draw
            top = getattr(getattr(d, "bar", None), "height", 0)
            white, ink, muted = 0xFFFF, 0x18E4, 0x7BF1
            d.fill_rect(0, top, 240, 320 - top, white)
            d.text("Wi-Fi setup", (240 - d.text_width("Wi-Fi setup", d.font_medium)) // 2,
                   top + 6, d.font_medium, ink, white)
            y = top + 34
            if show_qr:
                q = encode(wifi_text(self.ssid, self.key))
                scale = min(6, 176 // q.size)
                side = q.size * scale
                draw(d, q, (240 - side) // 2, y, scale, 0x0000, white, 0)
                y += side + 10
            for i, s in enumerate(lines):
                f = d.font_small
                while len(s) > 1 and d.text_width(s, f) > 232:
                    s = s[:-1]
                d.text(s, (240 - d.text_width(s, f)) // 2, y, f, color if i == 0 else muted, white)
                y += 18
        except Exception as e:
            print("wifi_setup: screen:", e)

    def show_join(self, note=None):
        lines = [note or "Scan with the phone camera",
                 "Network: " + self.ssid,
                 "Password: " + self.key,
                 "Then open http://%s" % self.ip]
        self.screen(lines, True, 0xE186 if note else 0x18E4)

    # --- main loop ----------------------------------------------------------

    def run(self, timeout):
        self.screen(["Looking for networks..."], False)
        self.scan()
        self.start_ap()
        self.open_sockets()
        self.show_join()
        poll = select.poll()
        poll.register(self.dns, select.POLLIN)
        poll.register(self.web, select.POLLIN)
        deadline = time.ticks_add(time.ticks_ms(), timeout * 1000)
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            for obj, _ in poll.poll(500):
                if obj is self.dns:
                    self.answer_dns()
                    continue
                found = self.serve_http()
                deadline = time.ticks_add(time.ticks_ms(), timeout * 1000)
                if not found:
                    continue
                ssid, password = found
                self.screen(["Connecting to " + ssid + "..."], False)
                try:
                    result = self.try_network(ssid, password)
                except RuntimeError as e:
                    self.msg = str(e).replace("Wi-Fi: ", "") + " Try again."
                    self.start_ap()  # back on its channel if the attempt moved it
                    self.show_join(str(e).replace("Wi-Fi: ", ""))
                    continue
                self.close()
                self.screen(["Connected to " + ssid,
                             "IP address: " + result[0],
                             "Saved: WiFi().connect() needs",
                             "no name or password now."], False, 0x2DAA)
                return result
        self.close()
        self.screen(["Wi-Fi setup stopped (no visits).",
                     "Run pibody.wifi_setup() again."], False)
        return None


def run(timeout=PAGE_TIMEOUT):
    """Set up Wi-Fi from a phone. Returns ifconfig() once connected, or None
    if nobody opened the page for timeout seconds. Ctrl-C stops it."""
    s = _Setup()
    try:
        return s.run(timeout)
    finally:
        s.close()
