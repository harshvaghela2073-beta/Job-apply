"""The candidate profile, stored in the database as ``ProfileField`` rows, and the resume file.

Every write regenerates the profile mirror file.
"""

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import FileResponse

from app.api.deps import SessionDep, SettingsDep
from app.schemas import ProfileFieldsWrite, ProfileRead
from app.services.mirror import refresh_mirror
from app.services.profile import build_profile, delete_field, resume_file, upsert_fields

router = APIRouter(prefix="/profile", tags=["profile"])


@router.get("", response_model=ProfileRead)
def get_profile(session: SessionDep, settings: SettingsDep) -> ProfileRead:
    return build_profile(session, settings.resume_path)


@router.put("/fields", response_model=ProfileRead)
def put_fields(body: ProfileFieldsWrite, session: SessionDep, settings: SettingsDep) -> ProfileRead:
    upsert_fields(session, body.fields)
    refresh_mirror(session, settings)
    return build_profile(session, settings.resume_path)


@router.delete("/fields/{key}", status_code=204, response_class=Response)
def remove_field(key: str, session: SessionDep, settings: SettingsDep) -> Response:
    if not delete_field(session, key):
        raise HTTPException(status_code=404, detail=f"Profile field '{key}' not found")
    refresh_mirror(session, settings)
    return Response(status_code=204)


@router.get(
    "/resume",
    response_class=FileResponse,
    responses={200: {"content": {"application/pdf": {}}}, 404: {"description": "No resume file"}},
)
def get_resume(settings: SettingsDep) -> FileResponse:
    path = resume_file(settings.resume_path)
    if path is None:
        raise HTTPException(status_code=404, detail="Resume file not found")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=path.name,
        content_disposition_type="inline",
    )
