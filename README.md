### PiBody Libraries
Libraries for PiBody alongside wrappers that facilitate work and extend the functionality of some modules.
Additionally, included Demo projects.
[online-ide](https://code.artisan.education)

#### Wi-Fi
Set up Wi-Fi once from a phone, then connect without a name or password:
```python
import pibody
pibody.wifi_setup()        # QR code on the screen: scan it with the phone camera,
                           # pick the network on the page that opens, type its password

from pibody import WiFi
wifi = WiFi()
wifi.connect()             # the saved networks, strongest first
wifi.connect("Home", "password")             # as before
wifi.connect("Home", "password", save=True)  # and keep it for next time
wifi.saved(); wifi.forget("Home"); wifi.forget()
```
- Saved networks live in `/wifi.json` (up to 5, newest first).
- `connect()` stops right away with "wrong password" or "network not found"
  instead of waiting for the timeout. The error is still a `RuntimeError`.
- `get_time()` also sets the board clock, so `time.localtime()` is right afterwards.
- `status()` returns `"time": None` when there is no internet.
- Web requests (`get_currency`, `get_temperature`, `get_ip_info`, `TelegramBot`)
  give up after 10-15 s instead of hanging.
- `wifi_setup()` opens the network "<board name> setup" (WPA2, a new 8-digit
  password each time, shown on the screen and in the terminal). Phones never
  share their saved Wi-Fi passwords, so the password is typed once. It stops
  after 5 minutes without visits, or on Ctrl-C.
- In BLE mode the top bar shows a Wi-Fi icon while Wi-Fi is on: grey when on
  but not connected, amber while connecting, dark when connected, red after a
  failed connect, blue during `wifi_setup()`. Wi-Fi and BLE run together; the
  BLE IDE link stays up.
