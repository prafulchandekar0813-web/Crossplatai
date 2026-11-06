"""workbench_client.py - added to the backend files you download from Manage app (next to app_config.json). Standard library only.

    from workbench_client import Client
    client = Client()                       # reads app_config.json next to this file
    result = client.sync("products")        # call at start-up, and again after result.retry_in seconds
    result.items      # list of {title, description, price, image_url, in_stock}
    result.settings   # {title, accent, announcement}
    result.message    # text to show the user (or "")
    result.retry_in   # seconds until the app should call sync() again (0 = no need)
    ok, msg = client.submit("orders", {"name": "Ana", "item": "Tea", "qty": 2})   # lands in the owner's inbox

It remembers the last good answer on disk, so the app still shows products offline.
"""
import json
import os
import secrets
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


class Result:
    def __init__(self, items=None, settings=None, message="", retry_in=0, offline=False, revision=0):
        self.items, self.settings, self.message = items or [], settings or {}, message
        self.retry_in, self.offline, self.revision = retry_in, offline, revision


def _data_dir():
    for base in (os.environ.get("ANDROID_PRIVATE"), os.environ.get("APPDATA"), os.path.expanduser("~/.local/share"), HERE):
        if base and os.path.isdir(base):
            path = os.path.join(base, "workbench-app")
            try:
                os.makedirs(path, exist_ok=True)
                return path
            except OSError:
                continue
    return None


class Client:
    def __init__(self, config_path=None, version="", platform=""):
        with open(config_path or os.path.join(HERE, "app_config.json"), encoding="utf-8") as fh:
            self.config = json.load(fh)
        self.version, self.platform = version, platform or ("apk" if os.environ.get("ANDROID_PRIVATE") else "exe")
        self.dir = _data_dir()
        self.device_id = self._device_id()

    def _file(self, name):
        return os.path.join(self.dir, f"{self.config['app_id']}-{name}") if self.dir else None

    def _device_id(self):
        """A random id made on first start. It is not tied to the person, the phone or the account."""
        path = self._file("device")
        try:
            with open(path) as fh:
                value = fh.read().strip()
            if len(value) == 24:
                return value
        except (OSError, TypeError):
            pass
        value = secrets.token_hex(12)
        try:
            with open(path, "w") as fh:
                fh.write(value)
        except (OSError, TypeError):
            pass
        return value

    def sync(self, collection="products", timeout=8):
        url = f"{self.config['api_base']}/api/catalog/{self.config['app_id']}/{collection}"
        req = urllib.request.Request(url, headers={"X-Device-Id": self.device_id, "X-App-Version": self.version, "X-Platform": self.platform})
        cache = self._file(f"{collection}.json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                data = json.loads(res.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError):
            return self._cached(cache)
        if cache:
            try:
                with open(cache, "w", encoding="utf-8") as fh:
                    json.dump(data, fh)
            except OSError:
                pass
        return self._result(data)

    def submit(self, collection, data, timeout=8):
        """Send an order, message or sign-up to the owner's inbox (Manage app). Returns (ok, message_for_the_user)."""
        url = f"{self.config['api_base']}/api/catalog/{self.config['app_id']}/submit/{collection}"
        req = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "X-Device-Id": self.device_id})
        try:
            with urllib.request.urlopen(req, timeout=timeout):
                return True, "Sent."
        except urllib.error.HTTPError as e:
            try:
                return False, json.loads(e.read().decode("utf-8")).get("error", "Could not send.")
            except (ValueError, OSError):
                return False, "Could not send."
        except (urllib.error.URLError, OSError):
            return False, "Can't reach the server. Check your internet connection and try again."

    def _cached(self, cache):
        try:
            with open(cache, encoding="utf-8") as fh:
                r = self._result(json.load(fh))
            r.offline, r.message = True, "You are offline. Showing the last saved version."
            return r
        except (OSError, TypeError, ValueError):
            return Result(offline=True, message="Can't reach the server. Check your internet connection.", retry_in=30)

    @staticmethod
    def _result(data):
        upd = data.get("update") or {}
        wait = int(upd.get("seconds_left") or 0)
        message = ""
        if upd.get("pending"):
            message = "Updating app. Please wait, or close and reopen in " + (f"{(wait + 59) // 60} minute" + ("s" if wait > 60 else "") if wait >= 30 else f"{max(wait, 5)} seconds") + "."
        return Result(data.get("items"), data.get("settings"), message, (wait + 2) if upd.get("pending") else 0, False, data.get("revision", 0))
