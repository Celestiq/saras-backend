# app/services/auth_service.py
from fastapi import HTTPException, status
from supabase import Client
from supabase.lib.client_options import ClientOptions

from app.domain.models import UserCredentials

class AuthService:
    """Handles authentication logic using Supabase GoTrue client."""

    def __init__(self, supabase: Client):
        self.sb = supabase

    def sign_up(self, credentials: UserCredentials) -> dict:
        """Signs up a new user."""
        try:
            # The sign_up method returns a session object upon success
            session = self.sb.auth.sign_up({
                "email": credentials.email,
                "password": credentials.password,
                "options": {
                    "data": {
                        "full_name": credentials.name
                    }
                }
            })
            # Supabase returns the session directly for the user to be logged in
            return session.model_dump()
        except Exception as e:
            # Catch any exception from Supabase auth and treat it as a client error
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e)
            )

    def sign_in(self, credentials: UserCredentials) -> dict:
        """Signs in an existing user."""
        try:
            session = self.sb.auth.sign_in_with_password({
                "email": credentials.email,
                "password": credentials.password,
            })
            return session.model_dump()
        except Exception as e:
            # Catch any exception and treat it as an authentication failure
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(e)
            )
        
    def delete_user(self, email: str):
        """
        Deletes a user by their email using admin privileges.
        NOTE: This requires the Supabase client to be initialized with the service_role_key.
        """
        try:
            # To delete a user, we first need their ID.
            # We can get the user's data from the admin interface by their email.
            admin_user_list = self.sb.auth.admin.list_users()
            user_to_delete = None
            for user in admin_user_list:
                if user.email == email:
                    user_to_delete = user
                    break
            
            if user_to_delete:
                # If the user is found, delete them using their ID.
                self.sb.auth.admin.delete_user(id=user_to_delete.id)
                print(f"Successfully deleted user from Supabase: {email}")
            else:
                # If the user is not found, it's not an error in a teardown context.
                print(f"User with email {email} not found. Nothing to delete.")
        except Exception as e:
            # Catch any other potential exceptions
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"An unexpected error occurred while trying to delete user: {str(e)}"
            )