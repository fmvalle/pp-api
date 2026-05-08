import json
import os

import firebase_admin
from firebase_admin import credentials

from app.core.config import settings


def ensure_firebase_initialized() -> None:
    """Garante Admin SDK antes de auth.verify_id_token / create_user / etc.

    Chamado de forma lazy para o contentor aceitar tráfego (ex. /health no Cloud Run)
    mesmo que credenciais Firebase falhem só em rotas que precisam delas.
    """
    init_firebase()


def init_firebase() -> None:
    if firebase_admin._apps:
        return

    opts = {"projectId": settings.firebase_project_id}

    if settings.firebase_credentials_path:
        with open(settings.firebase_credentials_path, "r", encoding="utf-8") as f:
            info = json.load(f)
        _validate_project_id(info.get("project_id"))
        cred = credentials.Certificate(info)
        firebase_admin.initialize_app(cred, opts)
        return

    if settings.firebase_credentials_json:
        info = json.loads(settings.firebase_credentials_json)
        _validate_project_id(info.get("project_id"))
        cred = credentials.Certificate(info)
        firebase_admin.initialize_app(cred, opts)
        return

    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if path and os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            info = json.load(f)
        _validate_project_id(info.get("project_id"))
        cred = credentials.Certificate(info)
        firebase_admin.initialize_app(cred, opts)
        return

    raise RuntimeError(
        "Firebase Admin: defina FIREBASE_CREDENTIALS_PATH, FIREBASE_CREDENTIALS_JSON "
        "ou GOOGLE_APPLICATION_CREDENTIALS apontando para a service account."
    )


def _validate_project_id(credential_project_id: str | None) -> None:
    if not credential_project_id:
        raise RuntimeError("Firebase Admin: service account sem project_id.")
    if credential_project_id != settings.firebase_project_id:
        raise RuntimeError(
            "Firebase Admin: project_id divergente entre credencial e configuração. "
            f"credential.project_id={credential_project_id} != FIREBASE_PROJECT_ID={settings.firebase_project_id}"
        )
