import io
import uuid

import httpx
import pytest
from PIL import Image
from sqlalchemy import select
from workmemory.db.models import ChatMessage
from workmemory.main import create_app
from workmemory.pets.skins import MAX_UPLOAD_BYTES, SkinLibrary


@pytest.fixture
async def pet_client(runtime, tmp_path):
    library = SkinLibrary(tmp_path / "pets")
    app = create_app(runtime.settings, engine=runtime.engine, ollama=runtime.ollama, memory=runtime.memory,
                     run_worker=False, mcp_store=runtime.mcp.store, pet_library=library)
    async with app.router.lifespan_context(app):
        app.state.runtime.agent.launch = lambda identifier: None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                                     headers={"Authorization": "Bearer " + runtime.settings.api_token}) as client:
            yield client, library


def image():
    data = io.BytesIO()
    Image.new("RGBA", (24, 60), "red").save(data, format="PNG")
    return data.getvalue()


async def test_upload_select_conflict_preview_and_invalid_skin(pet_client):
    client, library = pet_client
    response = await client.post("/api/pet/skins", files={"image": ("角色.png", image(), "image/png")})
    assert response.status_code == 201, response.text
    skin = response.json()
    assert library.exists(skin["id"])
    state = (await client.get("/api/client/state")).json()
    assert state["pet_skin"] == "builtin-cat" and state["pet_motion_enabled"]
    change = {"pet_skin": skin["id"], "pet_motion_enabled": False, "expected_revision": state["revision"]}
    assert (await client.patch("/api/client/state", json=change)).status_code == 200
    assert (await client.patch("/api/client/state", json=change)).status_code == 409
    preview = await client.get("/api" + skin["preview"])
    assert preview.status_code == 200 and preview.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(preview.content)).size == (24, 60)
    assert (await client.get("/api" + skin["preview"], headers={"Authorization": ""})).status_code == 401
    assert (await client.post("/api/pet/skins", files={"image": ("fake.png", b"bad", "image/png")})).status_code == 422
    change.update(pet_skin="f" * 64, expected_revision=state["revision"] + 1)
    assert (await client.patch("/api/client/state", json=change)).status_code == 404
    assert (await client.get("/api/client/state")).json()["pet_skin"] == skin["id"]


async def test_upload_bounds_with_and_without_length(pet_client):
    client, library = pet_client
    headers = {"Content-Type": "multipart/form-data; boundary=synthetic", "Content-Length": str(MAX_UPLOAD_BYTES + 1)}
    assert (await client.post("/api/pet/skins", content=b"", headers=headers)).status_code == 413
    async def chunks():
        yield b'--synthetic\r\nContent-Disposition: form-data; name="image"; filename="a.png"\r\nContent-Type: image/png\r\n\r\n'
        for _ in range(34):
            yield b"x" * (1024 * 1024)
    response = await client.post("/api/pet/skins", content=chunks(), headers={"Content-Type": "multipart/form-data; boundary=synthetic"})
    assert response.status_code == 413, response.text
    assert not library.root.exists()


async def test_submission_receipt_does_not_resend(runtime, pet_client):
    client, _ = pet_client
    cid = (await client.post("/api/conversations", json={})).json()["id"]
    rid = str(uuid.uuid4())
    path = f"/api/conversations/{cid}/requests/{rid}"
    assert (await client.get(path)).json() == {"accepted": False}
    message = {"content": "合成草稿", "request_id": rid}
    assert (await client.post(f"/api/conversations/{cid}/messages", json=message)).status_code == 202
    assert (await client.get(path)).json() == {"accepted": True}
    await client.post(f"/api/conversations/{cid}/messages", json=message)
    async with runtime.sessions() as session:
        rows = (await session.scalars(select(ChatMessage).where(ChatMessage.conversation_id == uuid.UUID(cid)))).all()
        assert len(rows) == 1
    await client.delete(f"/api/conversations/{cid}")
    assert (await client.get(path)).status_code == 404
