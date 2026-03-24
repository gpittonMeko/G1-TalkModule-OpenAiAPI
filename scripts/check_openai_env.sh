#!/bin/bash
# Uso sul server: bash scripts/check_openai_env.sh
# Mostra solo lunghezza/prefisso della chiave (non la chiave intera).
set -e
cd "$(dirname "$0")/.." || exit 1
.venv/bin/python3 << 'PY'
from pathlib import Path
import os
from dotenv import load_dotenv
p = Path(".env").resolve()
load_dotenv(p)
k = os.getenv("OPENAI_API_KEY", "")
print("dotenv_path:", p)
print("exists:", p.exists())
print("key_len:", len(k))
print("starts_sk:", k.startswith("sk-"))
print("has_newline:", bool(k and ("\n" in k or "\r" in k)))
if k:
    print("prefix:", k[:12])
    print("suffix:", k[-4:])
PY
