# app/api/routes/wishes.py
from fastapi import APIRouter, Depends, HTTPException, status
from app.api.deps import current_user
from app.db.supabase import get_supabase
from app.services.planner_service import RoadmapPlanner
from app.domain.models import WishCreate, RefineRequest, WishResult
from app.core.logging import get_logger

# Instantiate the logger for this specific module
log = get_logger(__name__)

router = APIRouter(prefix="/wishes", tags=["wishes"])

@router.post("", response_model=WishResult, status_code=status.HTTP_201_CREATED)
def create_wish(payload: WishCreate, user: dict = Depends(current_user)):
    """
    Creates a new "wish" for a learning roadmap based on a user-provided topic.
    This triggers a potentially long-running background process to generate the roadmap.
    """
    log.info("Received request to create a new wish.")
    user_id = user.get("id")
    log.info(f"Roadmap creation requested by user_id: {user_id} for topic: '{payload.topic}'")
    try:
        planner = RoadmapPlanner(get_supabase())
        result = planner.create_roadmap(user_id=user_id, topic=payload.topic)
        wish_id = result.get("wish", {}).get("id")
        log.info(f"Successfully completed roadmap creation for wish_id: {wish_id}")
        return result
    except HTTPException as e:
        log.error(f"HTTP error during roadmap creation for user_id: {user_id}. Status: {e.status_code}, Detail: {e.detail}")
        raise e
    except Exception as e:
        log.critical(f"An unexpected server error occurred during roadmap creation for user_id: {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error while creating roadmap.")


@router.post("/{wish_id}/refine", summary="Refine an existing learning roadmap")
def refine_wish(wish_id: str, payload: RefineRequest, user: dict = Depends(current_user)):
    """
    Allows a user to provide instructions to modify or refine an existing
    learning roadmap.
    """
    user_id = user.get("id")
    log.info(f"Roadmap refinement requested by user_id: {user_id} for wish_id: {wish_id}")
    try:
        planner = RoadmapPlanner(get_supabase())
        result = planner.refine_roadmap(
            user_id=user_id,
            wish_id=wish_id,
            # roadmap=payload.roadmap,
            instructions=payload.instructions
        )
        log.info(f"Successfully refined roadmap for wish_id: {wish_id}")
        return result
    except HTTPException as e:
        log.error(f"HTTP error during roadmap refinement for wish_id: {wish_id}. Status: {e.status_code}, Detail: {e.detail}")
        raise e
    except Exception as e:
        log.critical(f"An unexpected server error occurred during roadmap refinement for wish_id: {wish_id}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error while refining roadmap.")