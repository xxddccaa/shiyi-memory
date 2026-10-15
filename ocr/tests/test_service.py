import io
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from workmemory_ocr.engine import Engine, InvalidImage
from workmemory_ocr.server import create_app


class StubEngine:
    def __init__(self, config):
        pass

    def recognize(self, data):
        return {"text": "合成测试", "line_count": 1, "engine": "paddleocr"}


@pytest.fixture
def config():
    return {
        "token": "test-token",
        "port": 8766,
        "detection_model": "PP-OCRv6_small_det",
        "recognition_model": "PP-OCRv6_small_rec",
        "cpu_threads": 2,
        "max_image_bytes": 1000,
    }


def test_image_endpoint_auth_size_and_no_cache(config):
    with TestClient(create_app(config, StubEngine)) as c:
        assert c.post("/ocr", content=b"abc").status_code == 401
        headers = {"Authorization": "Bearer test-token", "Content-Type": "image/png"}
        assert (
            c.post(
                "/ocr", headers={**headers, "Origin": "http://evil.test"}, content=b"abc"
            ).status_code
            == 403
        )
        assert c.post("/ocr", headers=headers, content=b"x" * 1001).status_code == 413
        assert (
            c.post("/ocr", headers={**headers, "Content-Type": "application/pdf"}).status_code
            == 415
        )
        response = c.post("/ocr", headers=headers, content=b"synthetic")
        assert response.json()["text"] == "合成测试"
        assert response.headers["cache-control"] == "no-store"


def test_second_image_rejected_while_inference_busy(config):
    started, finish = threading.Event(), threading.Event()

    class SlowEngine(StubEngine):
        def recognize(self, data):
            started.set()
            assert finish.wait(5)
            return super().recognize(data)

    with TestClient(create_app(config, SlowEngine)) as c, ThreadPoolExecutor() as pool:
        headers = {"Authorization": "Bearer test-token", "Content-Type": "image/png"}
        first = pool.submit(c.post, "/ocr", headers=headers, content=b"synthetic")
        try:
            assert started.wait(5)
            assert c.post("/ocr", headers=headers, content=b"synthetic").status_code == 429
        finally:
            finish.set()
        assert first.result().status_code == 200


def test_decoder_rejects_non_images_and_huge_png_without_inference():
    engine = Engine.__new__(Engine)
    with pytest.raises(InvalidImage):
        engine.recognize(b"not an image")
    png = io.BytesIO()
    Image.new("RGB", (2500, 2000), "white").save(png, format="PNG")
    with pytest.raises(InvalidImage):
        engine.recognize(png.getvalue())
