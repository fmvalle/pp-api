from fastapi import APIRouter

from app.v1.assessments_router import router as assessments_router
from app.v1.auth.router import router as auth_router
from app.v1.catalog_router import router as catalog_router
from app.v1.directory_router import router as directory_router
from app.v1.exam_report_router import router as exam_report_router
from app.v1.hardcut_router import router as hardcut_router
from app.v1.me.router import router as me_router
from app.v1.profiles_router import router as profiles_router

router = APIRouter()

router.include_router(auth_router, prefix="/auth", tags=["v1-auth"])
router.include_router(me_router, prefix="/me", tags=["v1-me"])
router.include_router(profiles_router, tags=["v1-profiles"])
router.include_router(catalog_router, tags=["v1-catalog"])
router.include_router(directory_router, tags=["v1-directory"])
router.include_router(assessments_router, tags=["v1-assessments"])
router.include_router(exam_report_router, tags=["v1-exam-reports"])
router.include_router(hardcut_router, tags=["v1-hardcut"])
