#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="${PROJECT_ROOT:-$HOME/Projects/JARVIS}"
HERMES_ENV="${HERMES_HOME:-$HOME/.hermes}/.env"
PROJECT_ENV="$PROJECT_ROOT/.env"
mkdir -p "$(dirname "$HERMES_ENV")" "$PROJECT_ROOT"

python3 - "$HERMES_ENV" "$PROJECT_ENV" <<'PY'
from pathlib import Path
import os, secrets, sys, tempfile

hermes_path, project_path = map(Path, sys.argv[1:3])

def parse(path):
    result = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            clean = line.strip()
            if clean and not clean.startswith("#") and "=" in clean:
                k, v = clean.split("=", 1); result[k.strip()] = v.strip()
    return result

def update(path, values):
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(values); output = []
    for line in lines:
        clean = line.strip()
        if clean and not clean.startswith("#") and "=" in clean:
            key = clean.split("=", 1)[0].strip()
            if key in pending:
                output.append(f"{key}={pending.pop(key)}"); continue
        output.append(line)
    if output and output[-1] != "": output.append("")
    output.extend(f"{k}={v}" for k, v in pending.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name+".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f: f.write("\n".join(output).rstrip()+"\n")
        os.chmod(tmp, 0o600); os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

existing = parse(hermes_path)
key = existing.get("API_SERVER_KEY") or secrets.token_urlsafe(48)
hermes = {
    "API_SERVER_ENABLED": "true",
    "API_SERVER_HOST": "127.0.0.1",
    "API_SERVER_PORT": "8642",
    "API_SERVER_CORS_ORIGINS": "http://127.0.0.1:8787,http://localhost:8787",
    "API_SERVER_KEY": key,
}
project = {
    "HERMES_API_BASE": "http://127.0.0.1:8642",
    "API_SERVER_KEY": key,
    "HERMES_PROFILE": "default",
    "JARVIS_PORT": "8787",
    "ELEVENLABS_API_KEY": parse(project_path).get("ELEVENLABS_API_KEY", ""),
    "ELEVENLABS_VOICE_ID": parse(project_path).get("ELEVENLABS_VOICE_ID", ""),
    "ELEVENLABS_MODEL_ID": "eleven_multilingual_v2",
    "ELEVENLABS_STT_MODEL_ID": "scribe_v1",
    "JARVIS_WHISPER_MODEL": "base",
}
update(hermes_path, hermes); update(project_path, project)
print(f"Registered Hermes API Server settings in {hermes_path}")
print(f"Mirrored bridge settings in {project_path}")
print("API key generated/preserved and stored with mode 0600 (not printed).")
PY

echo
echo "Registration complete. From a normal terminal, run:"
echo "  hermes gateway restart"
