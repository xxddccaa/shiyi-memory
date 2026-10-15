from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException

from workmemory.pets.skins import MAX_IMAGE_BYTES, MAX_UPLOAD_BYTES, SkinError

router = APIRouter(prefix="/api/pet")


class PetUploadLimit:
    """Bound multipart parsing, including requests without Content-Length."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") != "/api/pet/skins" or scope.get("method") != "POST":
            return await self.app(scope, receive, send)
        total = 0

        async def bounded():
            nonlocal total
            message = await receive()
            total += len(message.get("body", b""))
            if total > MAX_UPLOAD_BYTES:
                raise MultiPartException("PNG 文件不能超过 32 MiB")
            return message

        await self.app(scope, bounded, send)


@router.get("/skins")
async def skins(request: Request):
    return {"items": request.app.state.runtime.pet_library.list()}


@router.post("/skins", status_code=201)
async def import_skin(request: Request):
    try:
        return await read_skin_upload(request)
    except StarletteHTTPException as exc:
        if exc.detail == "PNG 文件不能超过 32 MiB":
            raise HTTPException(413, exc.detail) from exc
        raise


async def read_skin_upload(request: Request):
    async with request.form(max_files=1, max_fields=2, max_part_size=65536) as form:
        image = form.get("image")
        if not isinstance(image, UploadFile):
            raise HTTPException(422, "请选择 PNG 图片")
        if image.size and image.size > MAX_IMAGE_BYTES:
            raise HTTPException(413, "PNG 文件不能超过 32 MiB")
        data = await image.read(MAX_IMAGE_BYTES + 1)
        manifest = form.get("manifest")
        name = form.get("name", Path(image.filename or "我的形象.png").stem)
        if not isinstance(name, str) or (manifest is not None and not isinstance(manifest, str)):
            raise HTTPException(422, "形象名称或配置无效")
        if manifest is not None and len(manifest.encode()) >= 65536:
            raise HTTPException(413, "皮肤配置过大")
        try:
            return await request.app.state.runtime.pet_library.import_png(data, name, manifest)
        except SkinError as exc:
            raise HTTPException(422, str(exc)) from exc


@router.get("/skins/{identifier}/preview")
async def preview(identifier: str, request: Request):
    try:
        data = await request.app.state.runtime.pet_library.preview(identifier)
        return Response(data, media_type="image/png")
    except SkinError as exc:
        raise HTTPException(404, str(exc)) from exc
