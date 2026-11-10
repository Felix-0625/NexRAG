"""Pinned native WebUI dependency set with security-lock validation."""

from pathlib import Path


def webui_requirements(root: Path) -> str:
    """Replace vulnerable upstream pins with reviewed reproducible native versions."""
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
    # Keep the runtime slim while restoring its existing local speech-to-text path.
    # PyAV bundles decoding libraries, avoiding system FFmpeg, CUDA, torch,
    # and external transcription. faster-whisper 1.2.1 still passes metadata_errors,
    # which PyAV 19 removed (SYSTRAN/faster-whisper#1589).
    # Keep a compatible released decoder until Whisper ships the upstream fix.
    return (
        "\n".join(result)
        + "\npython-dotenv==1.2.4\nfilelock==4.2.5\nfaster-whisper==1.2.1\nav==18.1.0\n"
    )


def locked_webui_requirements(root: Path) -> str:
    """Reject stale locks instead of silently resolving new transitive dependencies."""
    requirements = webui_requirements(root)
    lock = (root / "scripts/webui-requirements.lock").read_text(encoding="utf8")

    def pins(text):
        """Extract normalized exact dependency pins from requirements text."""
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
