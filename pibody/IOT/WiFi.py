import network, time, socket, struct, urequests
NTP_HOST = 'pool.ntp.org'

WEEKDAYS = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday"
]

MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December"
]

CONFIG = "/wifi.json"  # saved networks: {"networks": [{"ssid": ..., "password": ...}]}
MAX_SAVED = 5
HTTP_TIMEOUT = 10      # seconds for web requests, so a bad network cannot hang a program

# network.WLAN.status() codes (cyw43)
_WRONG_PASSWORD, _NO_AP_FOUND, _CONNECT_FAIL = -3, -2, -1


def saved_networks():
    """Saved networks, most recently saved first: [(ssid, password), ...]."""
    try:
        import json
        with open(CONFIG) as f:
            nets = json.load(f).get("networks", [])
        return [(n["ssid"], n.get("password", "")) for n in nets if n.get("ssid")]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return []


def save_network(ssid, password=""):
    """Save (or move to the front) a network for connect() without arguments."""
    import json
    nets = [(s, p) for s, p in saved_networks() if s != ssid]
    nets.insert(0, (ssid, password))
    data = {"networks": [{"ssid": s, "password": p} for s, p in nets[:MAX_SAVED]]}
    tmp = CONFIG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    import os
    try:
        os.remove(CONFIG)
    except OSError:
        pass
    os.rename(tmp, CONFIG)


def forget_network(ssid=None):
    """Remove one saved network, or all of them when ssid is None."""
    import json, os
    nets = [] if ssid is None else [(s, p) for s, p in saved_networks() if s != ssid]
    if not nets:
        try:
            os.remove(CONFIG)
        except OSError:
            pass
        return
    with open(CONFIG, "w") as f:
        json.dump({"networks": [{"ssid": s, "password": p} for s, p in nets]}, f)


class WiFi:
    def __init__(self):
        self.wlan = network.WLAN(network.STA_IF)
        self.wlan.active(True)


    def connect(self, ssid: str = None, password: str = None, timeout: int = 20, save: bool = False):
        """Connect and return ifconfig(). Without ssid: the saved networks
        (pibody.wifi_setup() or save()), the strongest visible one first.
        Without password: the saved one for ssid (or none, an open network).
        save=True keeps ssid and password for next time."""
        if self.wlan.isconnected():
            if ssid is None or ssid == self.wlan.config("ssid"):
                print("Already connected.")
                print(f"IP address: {self.wlan.ifconfig()[0]}")
                return self.wlan.ifconfig()
            self.disconnect()  # another network asked for: switch

        self.wlan.active(True)
        if ssid is None:
            known = saved_networks()
            if not known:
                raise RuntimeError("No saved Wi-Fi network. Set one up with pibody.wifi_setup(), "
                                   "or call connect(ssid, password).")
            if len(known) > 1:
                known = self._by_signal(known)
            error = None
            for s, p in known:
                try:
                    return self._join(s, p, timeout)
                except RuntimeError as e:
                    error = e
            raise error

        if password is None:
            password = dict(saved_networks()).get(ssid, "")
        result = self._join(ssid, password, timeout)
        if save:
            save_network(ssid, password)
        return result

    def _by_signal(self, known):
        """Saved networks in range first, strongest first; the rest after."""
        try:
            seen = {}
            for n in self.wlan.scan():
                name = n[0].decode()
                seen[name] = max(seen.get(name, -999), n[3])
        except OSError:
            return known
        visible = sorted([k for k in known if k[0] in seen], key=lambda k: -seen[k[0]])
        return visible + [k for k in known if k[0] not in seen]

    def _clear(self):
        """Drop an address left from an earlier network. The driver keeps it
        after disconnect(), so isconnected() would still say yes for a while,
        and after a join it would say yes before the new network gives an
        address, with traffic failing. Restarting the interface clears it."""
        if self.wlan.ifconfig()[0] != "0.0.0.0":
            self.wlan.active(False)
            self.wlan.active(True)

    def _join(self, ssid, password, timeout):
        if not self.wlan.isconnected():
            self._clear()
        print(f"Connecting to {ssid}...")
        self.wlan.connect(ssid, password or None)

        start = time.ticks_ms()
        while not self.wlan.isconnected():
            st = self.wlan.status()
            if st in (_WRONG_PASSWORD, _NO_AP_FOUND, _CONNECT_FAIL):
                self.wlan.disconnect()
                if st == _WRONG_PASSWORD:
                    raise RuntimeError(f"Wi-Fi: wrong password for '{ssid}'.")
                if st == _NO_AP_FOUND:
                    raise RuntimeError(f"Wi-Fi: network '{ssid}' not found.")
                raise RuntimeError(f"Wi-Fi: could not connect to '{ssid}'.")
            if time.ticks_diff(time.ticks_ms(), start) > (timeout * 1000):
                self.wlan.disconnect()
                raise RuntimeError("Wi-Fi connection timed out.")
            time.sleep(0.25)

        print("Connected!")
        print("IP address:", self.wlan.ifconfig()[0])
        return self.wlan.ifconfig()

    def save(self, ssid: str, password: str = ""):
        """Save a network for connect() without arguments."""
        save_network(ssid, password)

    def forget(self, ssid: str = None):
        """Forget one saved network, or all of them."""
        forget_network(ssid)

    def saved(self):
        """Names of the saved networks."""
        return [s for s, _ in saved_networks()]

    def setup(self, timeout: int = 300):
        """Set up Wi-Fi from a phone (see pibody.wifi_setup)."""
        from .WiFiSetup import run
        return run(timeout)

    def disconnect(self):
        if self.wlan.isconnected():
            self.wlan.disconnect()
            print("Disconnected from Wi-Fi.")
        self._clear()

    def is_connected(self):
        return self.wlan.isconnected()

    def ip(self):
        return self.wlan.ifconfig()[0] if self.is_connected() else None
    
    def scan(self):
        nets = self.wlan.scan()
        return [(ssid.decode(), rssi) for ssid, bssid, ch, rssi, authmode, hidden in nets]

    
    def get_time(self, gmt=5, formatted=False):
        NTP_DELTA = 2208988800
        NTP_QUERY = bytearray(48)
        NTP_QUERY[0] = 0x1B
        gmt_offset = gmt * 3600
        addr = socket.getaddrinfo(NTP_HOST, 123)[0][-1]
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(1)
            res = s.sendto(NTP_QUERY, addr)
            msg = s.recv(48)
        finally:
            s.close()
        ntp_time = struct.unpack("!I", msg[40:44])[0]
        result = time.gmtime(ntp_time - NTP_DELTA + gmt_offset)
        try:  # the board clock too, so time.localtime() is right afterwards
            import machine
            machine.RTC().datetime((result[0], result[1], result[2], result[6],
                                    result[3], result[4], result[5], 0))
        except Exception:
            pass
        if formatted:
            return f"{MONTHS[result[1]-1]} {result[2]}, {result[0]} {WEEKDAYS[result[6]]} {result[3]:02}:{result[4]:02}:{result[5]:02}"
        else:
            return result 

    def get_currency(self, from_currency="USD", to_currency="KZT"):
        """
        Convert an amount from one currency to another using live exchange rates.

        Args:
            from_currency (str): Source currency code (e.g., "USD").
            to_currency (str): Target currency code (e.g., "KZT").

        Returns:
            float: Converted amount in target currency, or None if request fails.
        """
        try:
            url = f"https://open.er-api.com/v6/latest/{from_currency}"
            r = urequests.get(url, timeout=HTTP_TIMEOUT)
            data = r.json()
            r.close()
            return float(data["rates"][to_currency])
        except Exception as e:
            print("Error converting currency:", e)
            return None

    def get_temperature(self, city="Astana"):
        """
        Fetch current weather data for the given city.
        Defaults to Astana.

        Args:
            city (str): City name (default: "Astana")

        Returns:
            dict: Weather data including temperature, description, humidity.
        """
        try:
            # Open-Meteo is free and doesn’t require an API key
            url = f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1&language=en&format=json"
            r = urequests.get(url, timeout=HTTP_TIMEOUT)
            geo = r.json()
            r.close()
            lat = geo["results"][0]["latitude"]
            lon = geo["results"][0]["longitude"]

            url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true"
            r = urequests.get(url, timeout=HTTP_TIMEOUT)
            data = r.json()
            r.close()

            temp = data["current_weather"]["temperature"]
            return f"{temp} C"
        except Exception as e:
            print("Error fetching weather:", e)
            return None

    def get_ip_info(self):
        """
        Fetch public IP address and location info.

        Returns:
            dict: Dictionary with IP, city, region, country.
        """
        try:
            url = "http://ip-api.com/json"
            r = urequests.get(url, timeout=HTTP_TIMEOUT)
            data = r.json()
            r.close()
            return data
        except Exception as e:
            print("Error fetching IP info:", e)
            return None
    
    def status(self):
        return {
            "connected": self.is_connected(),
            "ip": self.ip(),
            "ssid": self.wlan.config('ssid') if self.is_connected() else None,
            "rssi": self.wlan.status('rssi') if self.is_connected() else None,
            "time": self._time_or_none()
        }

    def _time_or_none(self):
        if not self.is_connected():
            return None
        try:
            return self.get_time(formatted=True)
        except Exception:
            return None
   
