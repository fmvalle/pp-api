import json
import os
from urllib.parse import quote

import firebase_admin
from firebase_admin import credentials

from app.core.config import settings


def _normalize_firebase_private_key(raw: str) -> str:
    s = (raw or "").strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    return s.replace("\\n", "\n")


def _service_account_dict_from_env_fields() -> dict | None:
    """Equivalente ao JSON do Admin SDK a partir de FIREBASE_CLIENT_EMAIL + FIREBASE_PRIVATE_KEY."""
    email = (settings.firebase_client_email or "").strip()
    raw_pk = (settings.firebase_private_key or "").strip()
    if not email and not raw_pk:
        return None
    if email and not raw_pk:
        raise RuntimeError(
            "Firebase Admin: FIREBASE_CLIENT_EMAIL definido mas falta FIREBASE_PRIVATE_KEY."
        )
    if raw_pk and not email:
        raise RuntimeError(
            "Firebase Admin: FIREBASE_PRIVATE_KEY definido mas falta FIREBASE_CLIENT_EMAIL "
            "(ex.: firebase-adminsdk-…@parametro-pedagogico.iam.gserviceaccount.com)."
        )
    pk = _normalize_firebase_private_key(raw_pk)
    if "BEGIN PRIVATE KEY" not in pk or "END PRIVATE KEY" not in pk:
        raise RuntimeError(
            "Firebase Admin: FIREBASE_PRIVATE_KEY deve ser PEM RSA (BEGIN/END PRIVATE KEY)."
        )
    pid = (settings.firebase_project_id or "").strip()
    if not pid:
        raise RuntimeError("Firebase Admin: FIREBASE_PROJECT_ID em falta.")
    return {
        "type": "service_account",
        "project_id": pid,
        "private_key_id": "from-env",
        "private_key": pk,
        "client_email": email,
        "client_id": "",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_x509_cert_url": (
            "https://www.googleapis.com/robot/v1/metadata/x509/"
            + quote(email, safe="")
        ),
    }


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

    info_env = _service_account_dict_from_env_fields()
    if info_env is not None:
        _validate_project_id(info_env.get("project_id"))
        cred = credentials.Certificate(info_env)
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
        "Firebase Admin: defina FIREBASE_CREDENTIALS_PATH, FIREBASE_CREDENTIALS_JSON, "
        "FIREBASE_CLIENT_EMAIL + FIREBASE_PRIVATE_KEY (PEM), ou GOOGLE_APPLICATION_CREDENTIALS."
    )


def _validate_project_id(credential_project_id: str | None) -> None:
    if not credential_project_id:
        raise RuntimeError("Firebase Admin: service account sem project_id.")
    if credential_project_id != settings.firebase_project_id:
        raise RuntimeError(
            "Firebase Admin: project_id divergente entre credencial e configuração. "
            f"credential.project_id={credential_project_id} != FIREBASE_PROJECT_ID={settings.firebase_project_id}"
        )
