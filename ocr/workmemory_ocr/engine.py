"""PaddleOCR in a dedicated environment; all caches and weights stay in this checkout."""

import hashlib
import importlib.metadata
import io
import json
import os
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL_ROOT = ROOT / "models/paddleocr"


def configure_environment():
    for name, value in {
        "PADDLE_PDX_CACHE_HOME": str(MODEL_ROOT),
        "PADDLE_HOME": str(ROOT / ".cache/paddle"),
        "HF_HOME": str(ROOT / ".cache/huggingface"),
        "MODELSCOPE_CACHE": str(ROOT / ".cache/modelscope"),
        "XDG_CACHE_HOME": str(ROOT / ".cache"),
        "TMPDIR": str(ROOT / ".cache/ocr-tmp"),
        "PADDLE_PDX_MODEL_SOURCE": "BOS",
        "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True",
    }.items():
        os.environ[name] = value
    Path(os.environ["TMPDIR"]).mkdir(parents=True, exist_ok=True)


def read_config():
    return json.loads((ROOT / "config/ocr.json").read_text())


def versions():
    return {
        name: importlib.metadata.version(name) for name in ["paddleocr", "paddlepaddle", "paddlex"]
    }


class InvalidImage(ValueError):
    pass


class Engine:
    def __init__(self, config, *, download=False):
        configure_environment()
        import numpy as np
        from paddleocr import PaddleOCR

        self.config = config
        names = [config["detection_model"], config["recognition_model"]]
        kwargs = {}
        for role, name in zip(["detection", "recognition"], names, strict=True):
            path = MODEL_ROOT / "official_models" / name
            if not download or path.exists():
                if not (path / "inference.yml").is_file():
                    raise RuntimeError("OCR weights are missing; run scripts/ocr-prepare.sh")
                kwargs[f"text_{role}_model_dir"] = str(path)
            kwargs[f"text_{role}_model_name"] = name
        self.pipeline = PaddleOCR(
            **kwargs,
            device="cpu",
            cpu_threads=config["cpu_threads"],
            enable_mkldnn=False,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            text_recognition_batch_size=8,
            text_det_limit_side_len=1600,
            text_det_limit_type="max",
            text_rec_score_thresh=0.5,
        )
        # Warm up without inspecting the user's desktop.
        self.pipeline.predict(np.full((64, 256, 3), 255, dtype=np.uint8))

    def recognize(self, data):
        import numpy as np
        from PIL import Image, UnidentifiedImageError

        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.format != "PNG" or image.width * image.height > 4_500_000:
                    raise InvalidImage("Expected PNG up to 4.5 megapixels")
                image.load()
                # PaddleOCR ndarray input uses BGR, Pillow uses RGB.
                array = np.asarray(image.convert("RGB"))[:, :, ::-1].copy()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise InvalidImage("Invalid image") from exc
        results = self.pipeline.predict(array)
        lines = []
        characters = 0
        for result in results:
            for text, confidence in zip(result["rec_texts"], result["rec_scores"], strict=True):
                if confidence >= 0.5 and text.strip():
                    line = text.strip()[: 16000 - characters]
                    if line:
                        lines.append(line)
                        characters += len(line) + 1
                    if characters >= 16000:
                        break
        return {"text": "\n".join(lines)[:16000], "line_count": len(lines), "engine": "paddleocr"}


def prepare():
    config = read_config()
    model_names = [config["detection_model"], config["recognition_model"]]
    inventory = MODEL_ROOT / "inventory.json"
    current_versions = versions()
    if inventory.is_file():
        recorded = json.loads(inventory.read_text())
        valid = (
            recorded.get("versions") == current_versions and recorded.get("models") == model_names
        )
        for item in recorded.get("files", []):
            path = (ROOT / item["path"]).resolve()
            if not path.is_relative_to(MODEL_ROOT) or not path.is_file():
                valid = False
                break
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != item["sha256"]:
                    valid = False
                    break
        if valid and recorded.get("files"):
            print("PaddleOCR model inventory and SHA-256 checks passed")
            return
    Engine(config, download=True)
    files = []
    for name in model_names:
        for path in sorted((MODEL_ROOT / "official_models" / name).rglob("*")):
            if path.is_file() and not path.name.startswith("."):
                with path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                files.append(
                    {
                        "path": str(path.relative_to(ROOT)),
                        "size": path.stat().st_size,
                        "sha256": digest,
                    }
                )
    report = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "versions": current_versions,
        "models": model_names,
        "files": files,
    }
    temporary = inventory.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2))
    temporary.replace(inventory)
    print("PaddleOCR models ready; inventory saved inside models/paddleocr")


if __name__ == "__main__":
    prepare()
