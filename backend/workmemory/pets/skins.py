import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import uuid
import warnings
from pathlib import Path
from typing import Literal

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from workmemory.config import ROOT

MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_UPLOAD_BYTES = MAX_IMAGE_BYTES + 128 * 1024
SKIN_ID = re.compile(r"[a-fA-F0-9]{64}\Z")


class SkinError(ValueError):
    pass


class Animation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    frames: list[int] = Field(min_length=1, max_length=128)
    fps: float = Field(ge=1, le=24)


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=100)
    image: str = "image.png"
    columns: int = Field(ge=1, le=32)
    rows: int = Field(ge=1, le=32)
    presentation: Literal["static", "atlas"] = "atlas"
    animations: dict[str, Animation] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def valid(self):
        if not re.fullmatch(r"[^/\\\x00]+\.[pP][nN][gG]", self.image):
            raise ValueError("图片必须是皮肤目录内的 PNG")
        if "idle" not in self.animations:
            raise ValueError("缺少 idle 动画")
        for animation in self.animations.values():
            if any(frame < 0 or frame >= self.columns * self.rows for frame in animation.frames):
                raise ValueError("动画帧超出图集")
        if self.presentation == "static" and (self.columns != 1 or self.rows != 1):
            raise ValueError("单图皮肤必须为一帧")
        return self


def decode_png(data: bytes, *, atlas=False) -> Image.Image:
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise SkinError("PNG 文件不能超过 32 MiB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as probe:
                if probe.format != "PNG":
                    raise SkinError("请选择真正的 PNG 图片")
                maximum = 8192 if atlas else 4096
                if not (0 < probe.width <= maximum and 0 < probe.height <= maximum):
                    raise SkinError(f"图片宽高不能超过 {maximum} 像素")
                if getattr(probe, "n_frames", 1) != 1:
                    raise SkinError("请选择静态 PNG；动画请使用皮肤文件夹导入")
                probe.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                return image.convert("RGBA")
    except SkinError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise SkinError("PNG 损坏或无法解码，请换一张图片") from exc


class SkinLibrary:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else ROOT / "data/collector/desktop-pets"
        self.lock = asyncio.Lock()

    def _load(self, identifier):
        if not SKIN_ID.fullmatch(identifier):
            raise SkinError("形象不存在")
        root = self.root.resolve()
        folder = self.root / identifier
        if folder.is_symlink() or folder.resolve().parent != root:
            raise SkinError("皮肤路径无效")
        manifest = folder / "pet.json"
        try:
            if manifest.is_symlink() or manifest.stat().st_size >= 65536:
                raise SkinError("皮肤描述文件无效")
            skin = Manifest.model_validate_json(manifest.read_bytes())
            image = folder / skin.image
            if image.is_symlink() or image.resolve().parent != folder.resolve() or image.stat().st_size > MAX_IMAGE_BYTES:
                raise SkinError("皮肤图片路径或大小无效")
            return skin, image
        except (OSError, ValidationError) as exc:
            raise SkinError("形象文件缺失或损坏") from exc

    def exists(self, identifier):
        if identifier == "builtin-cat":
            return True
        try:
            self._load(identifier)
            return True
        except SkinError:
            return False

    def list(self):
        items = [{"id": "builtin-cat", "name": "默认猫咪", "presentation": "builtin", "preview": None}]
        if self.root.is_dir():
            for folder in sorted(self.root.iterdir()):
                if not SKIN_ID.fullmatch(folder.name):
                    continue
                try:
                    skin, _ = self._load(folder.name)
                    items.append(self.view(folder.name, skin))
                except SkinError:
                    continue
        return items

    @staticmethod
    def view(identifier, skin):
        return {"id": identifier, "name": skin.name, "presentation": skin.presentation,
                "preview": f"/pet/skins/{identifier}/preview"}

    def _import(self, data, name, manifest):
        try:
            skin = Manifest.model_validate_json(manifest) if manifest else None
        except ValidationError as exc:
            raise SkinError("皮肤配置无效，请检查版本、网格和动画帧") from exc
        image = decode_png(data, atlas=skin is not None and skin.presentation == "atlas")
        if skin and (image.width % skin.columns or image.height % skin.rows):
            raise SkinError("图集宽高必须能被网格整除")
        if not skin or skin.presentation == "static":
            bounds = image.getchannel("A").getbbox()
            if not bounds:
                raise SkinError("图片完全透明，请选择有可见角色的 PNG")
            image = image.crop(bounds)
            # A 240-point pet needs no 4096-pixel decoded texture on every frame.
            image.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
            skin = Manifest(name=(name or "我的形象").strip()[:100] or "我的形象", columns=1, rows=1,
                            presentation="static", animations={"idle": Animation(frames=[0], fps=1)})
        skin.image = "image.png"
        output = io.BytesIO()
        image.save(output, format="PNG")
        encoded = output.getvalue()
        if len(encoded) > MAX_IMAGE_BYTES:
            raise SkinError("解码后的 PNG 超过 32 MiB，请缩小图片")
        document = skin.model_dump()
        identity = {**document, "name": ""}  # Same art/config is reusable despite a renamed upload.
        identifier = hashlib.sha256(encoded + json.dumps(identity, sort_keys=True).encode()).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = self.root / identifier
        if destination.is_symlink():
            raise SkinError("皮肤目录不能是符号链接")
        if destination.exists():
            try:
                existing, existing_image = self._load(identifier)
                if existing_image.read_bytes() == encoded:
                    return self.view(identifier, existing)
            except (SkinError, OSError):
                pass  # Reimport repairs missing/corrupt resources under the same identifier.
        backup = self.root / (".replaced-" + uuid.uuid4().hex)
        temporary = Path(tempfile.mkdtemp(prefix=".import-", dir=self.root))
        try:
            for filename, content in [("image.png", encoded), ("pet.json", json.dumps(document, ensure_ascii=False).encode())]:
                path = temporary / filename
                path.write_bytes(content)
                path.chmod(0o600)
            if destination.exists():
                os.rename(destination, backup)
            try:
                os.rename(temporary, destination)
            except OSError:
                if backup.exists():
                    os.rename(backup, destination)
                raise
            if backup.is_dir():
                shutil.rmtree(backup)
            elif backup.exists():
                backup.unlink()
        except OSError as exc:
            raise SkinError("无法保存形象，请检查本地存储空间后重试") from exc
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return self.view(identifier, skin)

    async def import_png(self, data, name, manifest=None):
        async with self.lock:
            task = asyncio.create_task(asyncio.to_thread(self._import, data, name, manifest))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # Thread work continues after disconnect; retain the write lock until publication.
                await task
                raise

    def _preview(self, identifier):
        skin, path = self._load(identifier)
        image = decode_png(path.read_bytes(), atlas=skin.presentation == "atlas")
        w, h = image.width // skin.columns, image.height // skin.rows
        i = skin.animations["idle"].frames[0]
        image = image.crop(((i % skin.columns) * w, (i // skin.columns) * h,
                            (i % skin.columns + 1) * w, (i // skin.columns + 1) * h))
        image.thumbnail((256, 256), Image.Resampling.LANCZOS)
        result = io.BytesIO()
        image.save(result, format="PNG")
        return result.getvalue()

    async def preview(self, identifier):
        async with self.lock:
            return await asyncio.to_thread(self._preview, identifier)
