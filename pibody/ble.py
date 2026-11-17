# pibody.ble: simple Bluetooth links between boards (or a phone) for students.
#
#   from pibody import ble
#
#   # Board A: wait for others
#   ble.advertise("Robot")
#   msg = ble.receive()                 # waits for a message (a str)
#   ble.send("hello back")              # to every linked device
#
#   # Board B: find and link to board A
#   print(ble.scan())                   # ['Robot', ...]
#   ble.connect("Robot")
#   ble.send("hello")
#   print(ble.receive(timeout=5))       # None after 5 s without a message
#
#   ble.on_receive(lambda msg, sender: print(sender, msg))   # or a callback
#
# Messages are text lines (str; a "\n" inside a message splits it). They are
# sent in 20-byte pieces, so any length works; short ones are faster.
# Works next to the BLE REPL: the IDE keeps its link. The board has 3 BLE
# connections in total, so with the IDE connected 2 are left for links.
# While advertise() is on, the board advertises as the link name instead of
# for the IDE; stop_advertising() (or a soft reset) brings the IDE's back.
#
# Phones: any BLE app (e.g. nRF Connect) sees the service below; write text
# lines to RX and subscribe to TX.

import bluetooth
import time
from micropython import const

SERVICE = bluetooth.UUID("5a1e0001-7b1d-4c3a-9e7f-50b1bd1e0000")
RX = bluetooth.UUID("5a1e0002-7b1d-4c3a-9e7f-50b1bd1e0000")  # peers write here
TX = bluetooth.UUID("5a1e0003-7b1d-4c3a-9e7f-50b1bd1e0000")  # we notify here

_CENTRAL_CONNECT = const(1)
_CENTRAL_DISCONNECT = const(2)
_GATTS_WRITE = const(3)
_SCAN_RESULT = const(5)
_SCAN_DONE = const(6)
_PERIPHERAL_CONNECT = const(7)
_PERIPHERAL_DISCONNECT = const(8)
_GATTC_SERVICE_RESULT = const(9)
_GATTC_SERVICE_DONE = const(10)
_GATTC_CHARACTERISTIC_RESULT = const(11)
_GATTC_CHARACTERISTIC_DONE = const(12)
_GATTC_WRITE_DONE = const(17)
_GATTC_NOTIFY = const(18)

_CHUNK = const(20)
_HELLO = b"\x01"   # first line from a connecting board: its name
_MAX_QUEUE = const(32)


class _Peer:
    def __init__(self, conn, name, central):
        self.conn = conn
        self.name = name
        self.central = central  # True: we connected to it (we write to its RX)
        self.rx = None          # its RX value handle (when we are the central)
        self.tx = None
        self.buf = b""


_ble = None
_rx = _tx = None
_name = None          # our link name
_adv = False          # advertise() is on
_peers = {}           # conn handle -> _Peer
_queue = []           # (msg, sender)
_callback = None
_found = {}           # scan: addr -> [addr_type, name, has_service]
_scanning = False
_pending = None       # connect(): conn handle while discovering
_disc = {}            # discovery results
_last_sender = None


def _setup():
    global _ble, _rx, _tx
    if _ble is not None:
        return
    _ble = bluetooth.BLE()
    _ble.active(True)
    ((_rx, _tx),) = _ble.gatts_register_services((
        (SERVICE, (
            (RX, bluetooth.FLAG_WRITE | bluetooth.FLAG_WRITE_NO_RESPONSE),
            (TX, bluetooth.FLAG_READ | bluetooth.FLAG_NOTIFY),
        )),
    ))
    _ble.gatts_set_buffer(_rx, 128, True)
    _ble.irq(_irq)


def _ide_conn():
    """Connection handle of the BLE REPL link (not a peer), or None."""
    try:
        import bleterm
        return bleterm.status().get("conn_handle")
    except Exception:
        return None


def _adv_payload(name):
    n = name.encode()[:29]
    adv = b"\x02\x01\x06" + bytes((17, 0x07)) + bytes(SERVICE)
    resp = bytes((len(n) + 1, 0x09)) + n
    return adv, resp


def _start_adv():
    adv, resp = _adv_payload(_name)
    _ble.gap_advertise(100_000, adv_data=adv, resp_data=resp)


def _decode_name(data):
    i = 0
    while i + 1 < len(data):
        n = data[i]
        if n == 0:
            break
        if data[i + 1] in (0x08, 0x09):
            return bytes(data[i + 2:i + 1 + n]).decode()
        i += n + 1
    return None


def _has_service(data):
    want = bytes(SERVICE)
    i = 0
    while i + 1 < len(data):
        n = data[i]
        if n == 0:
            break
        if data[i + 1] in (0x06, 0x07) and want in bytes(data[i + 2:i + 1 + n]):
            return True
        i += n + 1
    return False


def _deliver(peer, data):
    peer.buf += data
    while b"\n" in peer.buf:
        line, peer.buf = peer.buf.split(b"\n", 1)
        if line.startswith(_HELLO):
            peer.name = line[1:].decode()
            continue
        try:
            msg = line.decode()
        except Exception:
            msg = str(line)
        if _callback is not None:
            try:
                _callback(msg, peer.name)
            except Exception as e:
                print("ble.on_receive:", e)
        else:
            if len(_queue) >= _MAX_QUEUE:
                _queue.pop(0)
            _queue.append((msg, peer.name))
    if len(peer.buf) > 1024:  # no newline for too long: drop
        peer.buf = b""


def _irq(event, data):
    global _scanning, _pending
    if event == _CENTRAL_CONNECT:
        conn = data[0]
        _peers[conn] = _Peer(conn, "?", False)
        if _adv:
            try:
                _start_adv()  # stay visible for more links
            except Exception:
                pass
    elif event in (_CENTRAL_DISCONNECT, _PERIPHERAL_DISCONNECT):
        _peers.pop(data[0], None)
        if _pending == data[0]:
            _disc["fail"] = True
        if event == _CENTRAL_DISCONNECT and _adv:
            try:
                _start_adv()
            except Exception:
                pass
    elif event == _GATTS_WRITE:
        conn, attr = data
        if attr == _rx:
            p = _peers.get(conn)
            if p is None:
                p = _peers[conn] = _Peer(conn, "?", False)
            _deliver(p, _ble.gatts_read(_rx))
    elif event == _SCAN_RESULT:
        addr_type, addr, adv_type, rssi, adv = data
        key = bytes(addr)
        e = _found.get(key)
        if e is None:
            e = _found[key] = [addr_type, None, False]
        name = _decode_name(adv)
        if name:
            e[1] = name
        if _has_service(adv):
            e[2] = True
    elif event == _SCAN_DONE:
        _scanning = False
    elif event == _PERIPHERAL_CONNECT:
        _pending = data[0]
        _disc["conn"] = data[0]
    elif event == _GATTC_SERVICE_RESULT:
        conn, start, end, uuid = data
        if uuid == SERVICE:
            _disc["range"] = (start, end)
    elif event == _GATTC_SERVICE_DONE:
        _disc["services"] = True
    elif event == _GATTC_CHARACTERISTIC_RESULT:
        conn, end, value, props, uuid = data
        if uuid == RX:
            _disc["rx"] = value
        elif uuid == TX:
            _disc["tx"] = value
    elif event == _GATTC_CHARACTERISTIC_DONE:
        _disc["chars"] = True
    elif event == _GATTC_WRITE_DONE:
        _disc["written"] = True
    elif event == _GATTC_NOTIFY:
        conn, handle, payload = data
        p = _peers.get(conn)
        if p is not None and handle == p.tx:
            _deliver(p, bytes(payload))


def _wait(key, deadline):
    while key not in _disc:
        if "fail" in _disc or time.ticks_diff(deadline, time.ticks_ms()) <= 0:
            return False
        time.sleep_ms(10)
    return True


# --- public API -------------------------------------------------------------

def advertise(name):
    """Be visible to other boards as `name` and accept their links."""
    global _name, _adv
    _setup()
    _name = str(name)
    _adv = True
    _start_adv()


def stop_advertising():
    """Stop being visible (links stay). The IDE's advertising comes back."""
    global _adv
    _adv = False
    if _ble is not None:
        _ble.gap_advertise(None)


def scan(seconds=3):
    """Names of boards that advertise() nearby, closest first is not guaranteed."""
    global _scanning
    _setup()
    _found.clear()
    _scanning = True
    _ble.gap_scan(int(seconds * 1000), 30000, 30000, True)
    while _scanning:
        time.sleep_ms(20)
    return sorted({e[1] for e in _found.values() if e[2] and e[1]})


def connect(name, timeout=10):
    """Link to the board advertising `name`. Returns True when linked."""
    global _pending
    _setup()
    for p in _peers.values():
        if p.name == name and p.central:
            return True
    deadline = time.ticks_add(time.ticks_ms(), int(timeout * 1000))
    target = None
    while target is None:
        scan(2)
        for addr, e in _found.items():
            if e[2] and e[1] == name:
                target = (e[0], addr)
                break
        if target is None and time.ticks_diff(deadline, time.ticks_ms()) <= 0:
            return False
    _disc.clear()
    _pending = None
    _ble.gap_connect(target[0], target[1])
    try:
        if not _wait("conn", deadline):
            return False
        conn = _disc["conn"]
        _ble.gattc_discover_services(conn, SERVICE)
        if not _wait("services", deadline) or "range" not in _disc:
            return False
        start, end = _disc["range"]
        _ble.gattc_discover_characteristics(conn, start, end)
        if not _wait("chars", deadline) or "rx" not in _disc or "tx" not in _disc:
            return False
        p = _Peer(conn, name, True)
        p.rx, p.tx = _disc["rx"], _disc["tx"]
        _ble.gattc_write(conn, p.tx + 1, b"\x01\x00", 1)  # notifications on
        _wait("written", deadline)
        _peers[conn] = p
        _write(p, _HELLO + (_name or "board").encode() + b"\n")
        return True
    finally:
        if _pending is not None and _pending not in _peers:
            try:
                _ble.gap_disconnect(_pending)
            except Exception:
                pass
        _pending = None


def _write(p, data):
    for i in range(0, len(data), _CHUNK):
        part = data[i:i + _CHUNK]
        for _ in range(100):
            try:
                if p.central:
                    _ble.gattc_write(p.conn, p.rx, part, 0)
                else:
                    _ble.gatts_notify(p.conn, _tx, part)
                break
            except OSError:
                time.sleep_ms(10)  # radio busy: try again
        else:
            raise OSError("ble: send to %s failed" % p.name)


def _targets(to):
    ide = _ide_conn()
    out = []
    for p in list(_peers.values()):
        if p.conn == ide:
            continue
        if to is None or p.name == to:
            out.append(p)
    return out


def send(msg, to=None):
    """Send a text message to every linked device, or only to name `to`.
    Returns how many devices it went to."""
    _setup()
    data = str(msg).replace("\n", " ").encode() + b"\n"
    peers = _targets(to)
    for p in peers:
        _write(p, data)
    return len(peers)


def receive(timeout=None):
    """Next message (str), waiting up to `timeout` seconds (None: forever).
    Returns None on timeout. ble.sender() tells who sent it."""
    global _last_sender
    _setup()
    deadline = None if timeout is None else time.ticks_add(time.ticks_ms(), int(timeout * 1000))
    while not _queue:
        if deadline is not None and time.ticks_diff(deadline, time.ticks_ms()) <= 0:
            return None
        time.sleep_ms(10)
    msg, _last_sender = _queue.pop(0)
    return msg


def sender():
    """Name of the device that sent the last message from receive()."""
    return _last_sender


def any():
    """Number of messages waiting for receive()."""
    return len(_queue)


def on_receive(callback):
    """Call callback(msg, sender) for every message instead of queueing it
    (None: back to receive()). Keep it short: no long sleeps inside."""
    global _callback
    _callback = callback


def connected():
    """Names of the linked devices ("?" for a phone or app that gave no name)."""
    return [p.name for p in _targets(None)]


def disconnect(name=None):
    """Drop the link to `name`, or all links."""
    for p in _targets(name):
        try:
            _ble.gap_disconnect(p.conn)
        except Exception:
            pass
