"""验证已发布的本地识别器与路由，不下载内容，也不调用外部服务。"""

import ast
import asyncio
import importlib.util
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

BACKEND = Path(__file__).resolve().parents[1] / "webui/backend/open_webui"


@pytest.fixture
def recognizer(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "local_stt_under_test", BACKEND / "utils/nexrag_local_stt.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("NEXRAG_LOCAL_STT", "true")
    return module


def load(module, model="tiny", **kwargs):
    return module.load_local_whisper(
        model,
        slim=True,
        auto_update=False,
        device="cuda",
        compute_type="float16",
        download_root="cache",
        **kwargs,
    )


def test_slim_requires_explicit_native_opt_in(recognizer, monkeypatch):
    monkeypatch.delenv("NEXRAG_LOCAL_STT")
    with pytest.raises(HTTPException, match="未启用"):
        load(recognizer)


def test_missing_dependency_is_actionable_local_error(recognizer, monkeypatch):
    def unavailable(_):
        raise ImportError("no faster_whisper")

    monkeypatch.setattr(recognizer.importlib, "import_module", unavailable)
    with pytest.raises(HTTPException, match="依赖未就绪") as error:
        load(recognizer)
    assert error.value.status_code == 503


def install_model(recognizer, monkeypatch, factory, downloader=None):
    downloader = downloader or (lambda model, **kwargs: str(Path("cache", model).resolve()))
    monkeypatch.setattr(
        recognizer.importlib,
        "import_module",
        lambda name: (
            SimpleNamespace(download_model=downloader)
            if name.endswith(".utils")
            else SimpleNamespace(WhisperModel=factory)
        ),
    )


def test_parallel_first_use_loads_one_cpu_int8_model(recognizer, monkeypatch):
    calls = []

    def factory(**kwargs):
        calls.append(kwargs)
        time.sleep(0.02)
        return object()

    install_model(recognizer, monkeypatch, factory)
    with ThreadPoolExecutor(max_workers=8) as pool:
        models = list(pool.map(lambda _: load(recognizer), range(8)))
    assert len(calls) == 1
    assert all(model is models[0] for model in models)
    assert calls[0]["device"] == "cpu"
    assert calls[0]["compute_type"] == "int8"
    assert calls[0]["local_files_only"] is True


def test_missing_weights_retry_download_and_failed_change_preserves_cache(recognizer, monkeypatch):
    calls = []
    model = object()

    def download(name, **kwargs):
        calls.append((name, kwargs.copy()))
        if name == "bad" or kwargs["local_files_only"]:
            raise RuntimeError("weights unavailable")
        return str(Path("cache", name).resolve())

    install_model(recognizer, monkeypatch, lambda **kwargs: model, download)
    assert load(recognizer) is model
    assert [call[1]["local_files_only"] for call in calls] == [True, False]
    with pytest.raises(HTTPException, match="模型加载失败"):
        load(recognizer, "bad")
    assert load(recognizer) is model
    assert len(calls) == 4


def test_catalog_name_does_not_resolve_to_checkout_base(recognizer, monkeypatch, tmp_path):
    (tmp_path / "base").mkdir()
    snapshot = tmp_path / "whisper-cache" / "base-snapshot"
    monkeypatch.chdir(tmp_path)
    downloads, constructors = [], []

    def download(name, **kwargs):
        downloads.append(name)
        return str(snapshot)

    install_model(
        recognizer, monkeypatch, lambda **kwargs: constructors.append(kwargs) or object(), download
    )
    load(recognizer, "base")
    assert downloads == ["base"]
    assert constructors[0]["model_size_or_path"] == str(snapshot)
    assert constructors[0]["model_size_or_path"] != str(tmp_path / "base")
    assert constructors[0]["local_files_only"] is True


def test_absolute_custom_model_requires_weights_and_never_downloads(
    recognizer, monkeypatch, tmp_path
):
    downloads = []
    install_model(
        recognizer,
        monkeypatch,
        lambda **kwargs: kwargs,
        lambda *args, **kwargs: downloads.append(args),
    )
    with pytest.raises(HTTPException, match="模型加载失败"):
        load(recognizer, str(tmp_path))
    (tmp_path / "model.bin").touch()
    model = load(recognizer, str(tmp_path))
    assert model["model_size_or_path"] == str(tmp_path)
    assert downloads == []


def test_lazy_inference_is_serialized(recognizer):
    active = 0
    maximum = 0
    counter = threading.Lock()

    def segments():
        nonlocal active, maximum
        with counter:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.01)
        yield SimpleNamespace(text="本地识别")
        with counter:
            active -= 1

    model = SimpleNamespace(transcribe=lambda *args, **kwargs: (segments(), None))
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(
            pool.map(
                lambda _: recognizer.transcribe_local_whisper(model, "recording.webm"), range(4)
            )
        )
    assert result == ["本地识别"] * 4
    assert maximum == 1


def shipped_function(name, namespace):
    tree = ast.parse((BACKEND / "routers/audio.py").read_text("utf8"))
    node = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<shipped-audio>", "exec"), namespace)
    return namespace[name]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "engine,bypass,expected_preprocessing",
    [("", False, False), ("openai", False, True), ("openai", True, False)],
)
async def test_local_pyav_bypass_does_not_change_external_preprocessing(
    engine, bypass, expected_preprocessing
):
    calls = []

    async def handler(*args):
        calls.append(args[1])
        return {"text": "recognized"}

    namespace = dict(
        Request=object,
        Optional=__import__("typing").Optional,
        asyncio=asyncio,
        os=os,
        log=logging.getLogger(__name__),
        HTTPException=HTTPException,
        Config=SimpleNamespace(get=AsyncMock(return_value=engine)),
        BYPASS_PYDUB_PREPROCESSING=bypass,
        require_local_stt=lambda **kwargs: None,
        USE_SLIM=True,
        MAX_FILE_SIZE=20_000_000,
        transcription_handler=handler,
        is_audio_conversion_required=lambda path: False,
        compress_audio=lambda path: calls.append("compress") or path,
        split_audio=lambda path, size: calls.append("split") or [path],
    )
    function = shipped_function("transcribe", namespace)
    result = await function(object(), "recording.webm")
    assert result["text"] == "recognized"
    assert ("compress" in calls) is expected_preprocessing
    assert ("split" in calls) is expected_preprocessing
    assert "recording.webm" in calls


@pytest.mark.asyncio
async def test_failed_local_config_does_not_persist_broken_engine():
    upsert = AsyncMock()

    def fail(*args):
        raise HTTPException(503, "本地语音模型加载失败")

    namespace = dict(
        Request=object,
        AudioConfigUpdateForm=object,
        Depends=lambda x: None,
        get_admin_user=lambda: None,
        USE_SLIM=True,
        asyncio=asyncio,
        Config=SimpleNamespace(get_many=AsyncMock(return_value={}), upsert=upsert),
        HTTPException=HTTPException,
        set_faster_whisper_model=fail,
        WHISPER_MODEL_AUTO_UPDATE=False,
    )
    function = shipped_function("update_audio_config", namespace)
    form = SimpleNamespace(
        stt=SimpleNamespace(ENGINE="", WHISPER_MODEL="bad"), tts=SimpleNamespace(ENGINE="openai")
    )
    with pytest.raises(HTTPException):
        await function(object(), form, object())
    upsert.assert_not_called()
