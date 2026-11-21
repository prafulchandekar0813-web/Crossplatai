"""Uploaded pictures are fitted to what the system needs instead of being refused."""
import base64
import io

import pytest
from PIL import Image

from llm_agent import imagefit
from tests.test_apps import make_zip


def pic(w, h, fmt="PNG", color=(200, 30, 30), exif_orientation=None):
    img = Image.new("RGB", (w, h), color)
    out = io.BytesIO()
    if exif_orientation:
        ex = Image.Exif(); ex[0x0112] = exif_orientation
        img.save(out, fmt, exif=ex)
    else:
        img.save(out, fmt)
    return out.getvalue()


def size(data):
    return Image.open(io.BytesIO(data)).size


def test_a_big_photo_is_shrunk_and_a_long_strip_is_trimmed():
    data, kind = imagefit.photo(pic(4000, 3000, "JPEG"))
    assert kind == "webp" and max(size(data)) == 1200 and size(data) == (1200, 900)
    assert size(imagefit.photo(pic(3000, 400))[0]) == (1200, 400)              # 7.5:1 strip becomes 3:1
    assert size(imagefit.photo(pic(300, 2000))[0]) == (300, 900)               # tall strip too
    assert size(imagefit.photo(pic(200, 100))[0]) == (200, 100)                # small pictures are never enlarged


def test_sideways_phone_photos_are_turned_upright_and_metadata_dropped():
    data = imagefit.photo(pic(400, 200, "JPEG", exif_orientation=6))[0]        # stored sideways: should be 200 x 400
    assert size(data) == (200, 400)
    assert not Image.open(io.BytesIO(data)).getexif()


def test_logo_is_cropped_or_kept_whole_and_always_square():
    wide = pic(1000, 400)
    cropped = imagefit.logo(wide)
    assert size(cropped) == (512, 512) and Image.open(io.BytesIO(cropped)).convert("RGB").getpixel((5, 5)) == (200, 30, 30)
    whole = Image.open(io.BytesIO(imagefit.logo(wide, whole=True))).convert("RGBA")
    assert whole.size == (512, 512) and whole.getpixel((256, 256))[:3] == (200, 30, 30) and whole.getpixel((256, 5))[3] == 255 and whole.getpixel((256, 5))[:3] == (255, 255, 255)
    assert size(imagefit.logo(pic(100, 100))) == (512, 512)                    # small logos are brought up to size


def test_bad_pictures_get_a_clear_message():
    for bad, text in [(b"not a picture", "could not be read"), (pic(10, 10), "at least"), (b"\0" * (imagefit.MAX_INPUT + 1), "under")]:
        with pytest.raises(imagefit.ImageError) as e:
            imagefit.photo(bad)
        assert text in e.value.message


async def test_product_pictures_logo_and_design_pictures_over_the_api(env):
    c = await env.signup("ida", plan="master")
    built = await c.post("/api/apps/create", json={"name": "Shop", "version": "1.0.0", "logo": base64.b64encode(pic(900, 300, "JPEG")).decode(), "logo_fit": "whole",
                                                    "project": base64.b64encode(make_zip({"app.py": "print(1)\n", "requirements.txt": "flask\n"})).decode()})
    assert built.status_code == 200, built.text
    anon = await env.new_client()
    assert size((await anon.get(f"/api/catalog/{built.json()['app_id']}/logo")).content) == (512, 512)
    auth = {"X-App-Id": built.json()["app_id"], "X-App-Password": built.json()["password"]}
    r = await c.post("/api/apps/manage/image", headers=auth, content=pic(3000, 2000, "JPEG"))      # far over the old 600 KB limit
    assert r.status_code == 200
    got = await anon.get(r.json()["image"])
    assert got.status_code == 200 and size(got.content) == (1200, 800)
    up = await c.post("/api/designs/image", content=pic(2500, 2500))
    assert up.status_code == 200
    url = up.json()["url"]
    served = await anon.get(url)
    assert served.status_code == 200 and size(served.content) == (1200, 1200) and served.headers["content-type"] == "image/webp"
    made = (await c.post("/api/designs", json={"name": "", "template": ""})).json()
    made["design"]["pages"][0]["blocks"] = [{"id": "pic", "type": "image", "src": url}, {"id": "bad", "type": "image", "src": "/api/designs/img/../../etc/passwd"}]
    saved = (await c.put(f"/api/designs/{made['id']}", json={"name": "x", "design": made["design"]})).json()
    blocks = (await c.get(f"/api/designs/{made['id']}")).json()["design"]["pages"][0]["blocks"]
    assert blocks[0]["src"] == url and blocks[1]["src"] == ""                  # only real uploads pass; anything else is dropped
    assert (await anon.get("/api/designs/img/0000000000000000.webp")).status_code == 404
    free = await env.signup("fay", plan="free")
    assert (await free.post("/api/designs/image", content=pic(50, 50))).status_code == 402
