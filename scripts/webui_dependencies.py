"""固定版本的原生 WebUI 依赖组合，以及安全锁文件校验。"""

from pathlib import Path


def webui_requirements(root: Path) -> str:
    """把存在漏洞的上游固定版本替换为经审查、可复现的原生固定版本。"""
    upstream = (root / "webui/backend/requirements-slim.txt").read_text(encoding="utf-8")
    replacements = {}
    for line in (
        (root / "scripts/webui-security-requirements.txt").read_text(encoding="utf-8").splitlines()
    ):
        requirement = line.split("#", 1)[0].strip()
        if requirement:
            name = requirement.split("==", 1)[0].split("[", 1)[0].lower().replace("_", "-")
            replacements[name] = requirement
    applied = set()
    result = []
    for line in upstream.splitlines():
        name = line.split("==", 1)[0].split("[", 1)[0].strip().lower().replace("_", "-")
        if name in replacements:
            result.append(replacements[name])
            applied.add(name)
        else:
            result.append(line)
    if set(replacements) != applied:
        raise RuntimeError("Security replacements no longer match the pinned upstream requirements")
    # 保持精简的 UI 运行时，同时恢复其现有的本地 STT 路径。
    # PyAV（faster-whisper 的依赖）自带音频解码库，因此不需要系统 FFmpeg、
    # CUDA、torch 或外部转写服务。faster-whisper 1.2.1 仍会向 PyAV 传入
    # metadata_errors，而 PyAV 19 已移除该参数（上游 SYSTRAN/faster-whisper#1589）。
    # 在 Whisper 发布包含上游修复的版本之前，保留兼容的已发布解码器版本。
    return (
        "\n".join(result)
        + "\npython-dotenv==1.2.4\nfilelock==4.2.5\nfaster-whisper==1.2.1\nav==18.1.0\n"
    )


def locked_webui_requirements(root: Path) -> str:
    """拒绝过期的锁文件，而不是悄悄解析出新的传递依赖包。"""
    requirements = webui_requirements(root)
    lock = (root / "scripts/webui-requirements.lock").read_text(encoding="utf8")

    def pins(text):
        result = {}
        for line in text.splitlines():
            entry = line.split("#", 1)[0].strip()
            if entry:
                name, version = entry.split("==", 1)
                result[name.split("[", 1)[0].lower().replace("_", "-")] = version.strip()
        return result

    locked = pins(lock)
    if any(locked.get(name) != version for name, version in pins(requirements).items()):
        raise RuntimeError("The native WebUI lock does not match its reviewed requirement pins")
    return lock
