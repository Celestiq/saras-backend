# app/services/user_service.py
from __future__ import annotations
from supabase import Client
from app.core.logging import get_logger

log = get_logger(__name__)

class UserService:
    def __init__(self, supabase: Client):
        self.sb = supabase

    def get_user_profile(self, user_id: str) -> dict | None:
        """
        Retrieves a user's profile from the 'profiles' table.
        """
        log.info(f"Fetching profile for user_id: {user_id}")
        try:
            profile_res = self.sb.table("profiles").select("full_name, avatar_url, email, credits").eq("user_id", user_id).single().execute()
            log.info(f"Supabase response: {profile_res}")
            if profile_res.data:
                log.info(f"Successfully found profile for user_id: {user_id}")
                return profile_res.data
            
            log.warning(f"No profile found in 'profiles' table for user_id: {user_id}")
            return None
        except Exception as e:
            log.error(f"Error fetching profile for user_id: {user_id}. Error: {e}")
            return None