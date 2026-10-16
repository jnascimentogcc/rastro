from fastapi import APIRouter

from rastro_api.deps import CurrentRepository
from rastro_api.schemas import WhoAmIOut

router = APIRouter(prefix="/v1/auth", tags=["auth"])


@router.get("/whoami", response_model=WhoAmIOut)
async def whoami(repository: CurrentRepository) -> WhoAmIOut:
    """Lets `rastro init` verify a token."""
    return WhoAmIOut(repository_id=repository.id, full_name=repository.full_name)
