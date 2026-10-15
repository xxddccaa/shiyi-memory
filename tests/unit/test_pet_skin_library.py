import io
import json

import pytest
from PIL import Image
from workmemory.pets.skins import SkinError, SkinLibrary


def png(size=(40, 80), color=(255, 80, 40, 255), format="PNG"):
    stream = io.BytesIO()
    Image.new("RGBA", size, color).save(stream, format=format)
    return stream.getvalue()


async def test_png_normalized_deduplicated_and_survives_original(tmp_path):
    library = SkinLibrary(tmp_path)
    original = Image.new("RGBA", (100, 100))
    original.paste(Image.new("RGBA", (20, 60), "red"), (40, 20))
    stream = io.BytesIO()
    original.save(stream, format="PNG")
    first = await library.import_png(stream.getvalue(), "角色")
    second = await library.import_png(stream.getvalue(), "改过文件名")
    assert first == second
    skin, path = library._load(first["id"])
    assert skin.presentation == "static" and Image.open(path).size == (20, 60)
    assert Image.open(io.BytesIO(await library.preview(first["id"]))).size == (20, 60)
    assert SkinLibrary(tmp_path).list()[1] == first
    assert not list(tmp_path.glob(".import-*"))


@pytest.mark.parametrize("data,match", [
    (png(color=(0, 0, 0, 0)), "完全透明"),
    (png(size=(4097, 1)), "4096"),
    (png(format="WEBP"), "真正的 PNG"),
    (b"not png", "损坏"),
    (png()[:50], "损坏"),
])
async def test_invalid_png_preserves_library(tmp_path, data, match):
    library = SkinLibrary(tmp_path / "skins")
    with pytest.raises(SkinError, match=match):
        await library.import_png(data, "无效")
    assert not library.root.exists()


async def test_apng_rejected(tmp_path):
    output = io.BytesIO()
    Image.new("RGBA", (5, 5), "red").save(output, format="PNG", save_all=True, append_images=[Image.new("RGBA", (5, 5), "blue")])
    with pytest.raises(SkinError, match="静态 PNG"):
        await SkinLibrary(tmp_path).import_png(output.getvalue(), "动画")


async def test_atlas_legacy_and_containment(tmp_path):
    library = SkinLibrary(tmp_path / "skins")
    manifest = {"version": 1, "name": "动画", "image": "cat.png", "columns": 2, "rows": 1,
                "animations": {"idle": {"frames": [1, 0], "fps": 6}}}
    result = await library.import_png(png((40, 80)), "动画", json.dumps(manifest))
    skin, image = library._load(result["id"])
    assert skin.presentation == "atlas"
    assert Image.open(io.BytesIO(await library.preview(result["id"]))).size == (20, 80)
    with pytest.raises(SkinError):
        await library.import_png(png(), "错误", json.dumps({**manifest, "image": "../outside.png"}))
    with pytest.raises(SkinError):
        await library.import_png(png((41, 80)), "错误", json.dumps(manifest))
    for key in ["../outside", "/tmp/file", "f" * 64]:
        assert not library.exists(key)
    outside = tmp_path / "outside.png"
    outside.write_bytes(png())
    image.unlink()
    image.symlink_to(outside)
    assert not library.exists(result["id"])
    assert len(library.list()) == 1
    with pytest.raises(SkinError):
        await library.preview(result["id"])


async def test_parallel_upload_is_one_complete_resource(tmp_path):
    import asyncio
    library = SkinLibrary(tmp_path)
    results = await asyncio.gather(*(library.import_png(png(), "合成形象") for _ in range(5)))
    assert len({r["id"] for r in results}) == 1
    assert len(library.list()) == 2


async def test_reimport_repairs_damaged_resource(tmp_path):
    library = SkinLibrary(tmp_path)
    first = await library.import_png(png(), "角色")
    _, path = library._load(first["id"])
    path.write_bytes(b"damaged")
    second = await library.import_png(png(), "角色")
    assert second["id"] == first["id"]
    assert Image.open(io.BytesIO(await library.preview(first["id"]))).size == (40, 80)
    assert not list(tmp_path.glob(".replaced-*"))


async def test_failed_publication_preserves_existing_resource(tmp_path, monkeypatch):
    import os
    library = SkinLibrary(tmp_path)
    first = await library.import_png(png(), "角色")
    _, path = library._load(first["id"])
    path.write_bytes(b"original damaged data")
    rename = os.rename
    def failed_publish(source, target):
        if source.name.startswith(".import-"):
            raise OSError("synthetic disk failure")
        return rename(source, target)
    monkeypatch.setattr(os, "rename", failed_publish)
    with pytest.raises(SkinError, match="无法保存"):
        await library.import_png(png(), "角色")
    assert path.read_bytes() == b"original damaged data"
    assert not list(tmp_path.glob(".import-*"))
    assert not list(tmp_path.glob(".replaced-*"))
