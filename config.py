import json
import os
from typing import Any, Dict


def _load_local_secrets(base_dir: str) -> Dict[str, Any]:
    path = os.path.join(base_dir, "instance", "local_secrets.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


class Config:
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))
    _LOCAL_SECRETS = _load_local_secrets(BASE_DIR)

    SECRET_KEY = (
        os.environ.get("SECRET_KEY")
        or _LOCAL_SECRETS.get("SECRET_KEY")
        or "replace-with-env-secret-key"
    )

    UPLOAD_FOLDER = os.path.join(BASE_DIR, "instance", "uploads")

    DB_HOST = os.environ.get("DB_HOST") or _LOCAL_SECRETS.get("DB_HOST") or ""
    DB_USER = os.environ.get("DB_USER") or _LOCAL_SECRETS.get("DB_USER") or ""
    DB_PASS = os.environ.get("DB_PASS") or _LOCAL_SECRETS.get("DB_PASS") or ""
    DB_NAME = os.environ.get("DB_NAME") or _LOCAL_SECRETS.get("DB_NAME") or ""

    ADMIN_USERNAME = (
        os.environ.get("ADMIN_USERNAME")
        or _LOCAL_SECRETS.get("ADMIN_USERNAME")
        or "admin"
    )
    ADMIN_PASSWORD = (
        os.environ.get("ADMIN_PASSWORD")
        or _LOCAL_SECRETS.get("ADMIN_PASSWORD")
        or "replace-with-env-admin-password"
    )

    BRIGHTDATA_ZONE = (
        os.environ.get("BRIGHTDATA_ZONE")
        or _LOCAL_SECRETS.get("BRIGHTDATA_ZONE")
        or "unblocked"
    )
    BRIGHTDATA_API_KEY = (
        os.environ.get("BRIGHTDATA_API_KEY")
        or _LOCAL_SECRETS.get("BRIGHTDATA_API_KEY")
        or ""
    )


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False


config = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "default": DevelopmentConfig,
}
