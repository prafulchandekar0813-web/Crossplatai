"""Create the Flutter platform folders for this app and fill in its name, package id, colour, logo and address.
Reads everything from environment variables (set by the workflow from validated inputs)."""
import json
import os
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path

app_id, api_base = os.environ["APP_ID"], os.environ["API_BASE"].rstrip("/")
name, package, accent = os.environ["APP_NAME"], os.environ["PACKAGE_ID"], os.environ.get("ACCENT") or "#2563eb"

assert re.fullmatch(r"[a-z0-9]{3,32}", app_id), "bad app id"
assert re.fullmatch(r"https://[A-Za-z0-9.\-]+(:\d+)?", api_base), "bad address"
assert re.fullmatch(r"[A-Za-z0-9 ._-]{1,30}", name), "bad name"
assert re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){2,4}", package), "bad package id"
assert re.fullmatch(r"#[0-9a-fA-F]{6}", accent), "bad colour"

org, project = package.rsplit(".", 1)
out = Path("build_app")
if out.exists():
    shutil.rmtree(out)
subprocess.run(["flutter", "create", "--org", org, "--project-name", project, "--platforms", "android,windows", str(out)], check=True)
shutil.rmtree(out / "lib")
shutil.copytree("shell/lib", out / "lib")
shutil.copytree("shell/assets", out / "assets", dirs_exist_ok=True)
pub = Path("shell/pubspec.yaml").read_text().replace("name: shell_app", f"name: {project}", 1)
(out / "pubspec.yaml").write_text(pub)
(out / "assets" / "app_config.json").write_text(json.dumps({"app_id": app_id, "api_base": api_base, "name": name, "accent": accent}))
try:
    urllib.request.urlretrieve(f"{api_base}/api/catalog/{app_id}/logo", out / "assets" / "logo.png")
except Exception as e:  # keep the placeholder icon rather than fail the build
    print("logo not downloaded:", e)

manifest = out / "android/app/src/main/AndroidManifest.xml"
text = re.sub(r'android:label="[^"]*"', lambda m: f'android:label="{name}"', manifest.read_text(), count=1)
if "android.permission.INTERNET" not in text:   # Flutter's release manifest has no internet permission; without it the app cannot reach anything
    text = text.replace("<application", '<uses-permission android:name="android.permission.INTERNET"/>\n    <application', 1)
manifest.write_text(text)
main_cpp = out / "windows/runner/main.cpp"
main_cpp.write_text(re.sub(r'L"' + re.escape(project) + r'"', lambda m: f'L"{name}"', main_cpp.read_text()))
