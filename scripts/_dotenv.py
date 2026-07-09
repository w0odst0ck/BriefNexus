"""Simple .env loader — no dependencies, sets os.environ."""
import os

__all__ = ["load_project_env"]


def load_project_env():
    """
    Load .env from project root into os.environ.
    Only sets keys not already present (setdefault), so env vars still win.
    """
    # scripts/_dotenv.py → scripts/ → project root
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(root, ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
