# app/api/routes/users.py
from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client

from app.db.supabase import get_supabase
from app.services.user_service import UserService
from app.api.deps import current_user
from app.domain.models import UserProfile
from app.core.logging import get_logger

# Instantiate the logger for this specific module
log = get_logger(__name__)

router = APIRouter(prefix="/users", tags=["users"])

def get_user_service(supabase: Client = Depends(get_supabase)) -> UserService:
    return UserService(supabase)

@router.get("/me", response_model=UserProfile, summary="Get current user's profile")
def get_my_profile(
    user: dict = Depends(current_user),
    service: UserService = Depends(get_user_service)
):
    """
    Fetches the profile information for the currently authenticated user.
    """
    user_id = user.get("id")
    profile = service.get_user_profile(user_id)

    log.info(f"Fetched profile for user_id {user_id}: {profile}")
    
    if not profile:
        # Fallback to email if profile is not found
        return UserProfile(
            full_name="User", 
            email=user.get("email"), 
            avatar_url=None,
            credits=0
        )
        
    return UserProfile(
        full_name=profile.get("full_name"),
        email=profile.get("email"),
        avatar_url=profile.get("avatar_url"),
        credits=profile.get("credits", 0)
    )