from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.deps import AuthContext, get_auth_context
from app.v1._scope import is_admin_like

router = APIRouter(tags=["v1-hardcut"])


@router.get("/hardcut/readiness")
async def hardcut_readiness_v1(ctx: Annotated[AuthContext, Depends(get_auth_context)]):
    """Matriz operacional para flags FORCE_API_V1 por domínio no app."""
    domains = [
        {"domain": "auth/contexto", "status": "ready_for_hard_cut", "notes": "Fluxo Firebase + sessão v1 estabilizado."},
        {"domain": "profile", "status": "ready_for_hard_cut", "notes": "Contexto ativo e metadados cobertos."},
        {"domain": "schools/classrooms", "status": "ready_for_hard_cut", "notes": "Hierarquia + turmas: create em subárvore; list classrooms não vaza fora de effective_classroom_ids."},
        {"domain": "users/students/teachers", "status": "ready_for_hard_cut", "notes": "Admin lista por subárvore com school_id; GET aluno/professor por effective_school_ids + self."},
        {"domain": "assessments/schedules/attendance", "status": "ready_for_hard_cut", "notes": "Roster, assessment_school, turma↔avaliação/agenda; presença admin+professor da turma; vw_classroom_students."},
        {"domain": "reports/dashboards", "status": "ready_with_caveats", "notes": "Cobertos; validar KPIs esperados por produto."},
        {"domain": "teacher portal", "status": "ready_with_caveats", "notes": "Coberto; monitorar cenários de alta cardinalidade."},
        {"domain": "exam", "status": "ready_with_caveats", "notes": "Fluxo aluno via `/v1/exam/*` (linear); IRT/parada adaptativa da edge antiga não portada."},
        {"domain": "realtime", "status": "not_ready_for_hard_cut", "notes": "Não implementado no backend v1."},
    ]
    return {
        "environment": {
            "firebase_project_id_expected": "parametro-pedagogico",
            "auth_mode": "firebase_client + api_v1_session",
        },
        "consumer_context": {
            "person_id": str(ctx.person_id),
            "active_profile_id": str(ctx.active_profile_id),
            "role": ctx.role,
            "is_admin_like": is_admin_like(ctx.role),
        },
        "domains": domains,
    }

