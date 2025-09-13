# app/services/credit_service.py
from __future__ import annotations

from fastapi import HTTPException, status
from supabase import Client
from app.core.logging import get_logger

log = get_logger(__name__)

class CreditService:
    """
    Manages credit-related operations including purchasing credits and updating user credit balances.
    """
    
    def __init__(self, supabase: Client):
        """Initializes the service with a Supabase client."""
        self.sb = supabase

    def get_user_credits(self, user_id: str) -> int:
        """Get the current credit balance for a user."""
        log.info(f"Fetching credit balance for user_id: {user_id}")
        
        try:
            profile_res = self.sb.table("profiles").select("credits").eq("user_id", user_id).single().execute()
            
            if profile_res.data:
                credits = profile_res.data.get("credits", 0)
                log.info(f"User {user_id} has {credits} credits")
                return credits
            else:
                log.warning(f"No profile found for user_id: {user_id}")
                return 0
                
        except Exception as e:
            log.error(f"Error fetching credits for user_id: {user_id}. Error: {e}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
                detail="Failed to fetch user credits"
            )

    def add_credits(self, user_id: str, credits: int) -> dict:
        """Add credits to a user's account."""
        log.info(f"Adding {credits} credits to user_id: {user_id}")
        
        try:
            # Get current credits
            current_credits = self.get_user_credits(user_id)
            new_credits = current_credits + credits
            
            # Update the user's credit balance
            update_res = self.sb.table("profiles").update({
                "credits": new_credits
            }).eq("user_id", user_id).execute()
            
            if update_res.data:
                log.info(f"Successfully updated credits for user_id: {user_id}. New balance: {new_credits}")
                return {
                    "user_id": user_id,
                    "credits_added": credits,
                    "previous_balance": current_credits,
                    "new_balance": new_credits
                }
            else:
                log.error(f"Failed to update credits for user_id: {user_id}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to update user credits"
                )
                
        except HTTPException:
            raise
        except Exception as e:
            log.error(f"Error adding credits for user_id: {user_id}. Error: {e}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to add credits to user account"
            )

    def deduct_credits(self, user_id: str, credits: int) -> dict:
        """Deduct credits from a user's account."""
        log.info(f"Deducting {credits} credits from user_id: {user_id}")
        
        try:
            # Get current credits
            current_credits = self.get_user_credits(user_id)
            
            if current_credits < credits:
                log.warning(f"Insufficient credits for user_id: {user_id}. Current: {current_credits}, Required: {credits}")
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Insufficient credits. Current balance: {current_credits}, Required: {credits}"
                )
            
            new_credits = current_credits - credits
            
            # Update the user's credit balance
            update_res = self.sb.table("profiles").update({
                "credits": new_credits
            }).eq("user_id", user_id).execute()
            
            if update_res.data:
                log.info(f"Successfully deducted credits for user_id: {user_id}. New balance: {new_credits}")
                return {
                    "user_id": user_id,
                    "credits_deducted": credits,
                    "previous_balance": current_credits,
                    "new_balance": new_credits
                }
            else:
                log.error(f"Failed to update credits for user_id: {user_id}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to update user credits"
                )
                
        except HTTPException:
            raise
        except Exception as e:
            log.error(f"Error deducting credits for user_id: {user_id}. Error: {e}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to deduct credits from user account"
            )

    def create_credit_purchase_record(self, user_id: str, package_id: str, credits: int, price: float, payment_type: str, payment_id: str) -> dict:
        """Create a record of the credit purchase for tracking purposes."""
        log.info(f"Creating credit purchase record for user_id: {user_id}, package: {package_id}")
        
        try:
            purchase_record = {
                "user_id": user_id,
                "package_id": package_id,
                "credits": credits,
                "price": price,
                "payment_type": payment_type,
                "payment_id": payment_id,
                "status": "pending"
            }
            
            # Insert the purchase record
            insert_res = self.sb.table("credit_purchases").insert(purchase_record).execute()
            
            if insert_res.data:
                log.info(f"Successfully created credit purchase record: {insert_res.data[0]['id']}")
                return insert_res.data[0]
            else:
                log.error(f"Failed to create credit purchase record for user_id: {user_id}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to create credit purchase record"
                )
                
        except HTTPException:
            raise
        except Exception as e:
            log.error(f"Error creating credit purchase record for user_id: {user_id}. Error: {e}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create credit purchase record"
            )

    def update_credit_purchase_status(self, payment_id: str, status: str) -> dict:
        """Update the status of a credit purchase record."""
        log.info(f"Updating credit purchase status for payment_id: {payment_id} to {status}")
        
        try:
            update_res = self.sb.table("credit_purchases").update({
                "status": status
            }).eq("payment_id", payment_id).execute()
            
            if update_res.data:
                log.info(f"Successfully updated credit purchase status for payment_id: {payment_id}")
                return update_res.data[0]
            else:
                log.error(f"Failed to update credit purchase status for payment_id: {payment_id}")
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Credit purchase record not found"
                )
                
        except HTTPException:
            raise
        except Exception as e:
            log.error(f"Error updating credit purchase status for payment_id: {payment_id}. Error: {e}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to update credit purchase status"
            )
