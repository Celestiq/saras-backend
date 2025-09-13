# app/api/routes/credits.py
from fastapi import APIRouter, Depends, HTTPException, status, Request, BackgroundTasks
from typing import Dict, Any
from urllib.parse import urlencode

from app.db.supabase import get_supabase
from app.services.credit_service import CreditService
from app.services.paypal_service import PayPalService
from app.services.user_service import UserService
from app.api.deps import current_user
from app.core.config import settings
from app.domain.models import CreditPurchaseRequest, CreditPurchaseResponse
from app.core.logging import get_logger
from supabase import Client

log = get_logger(__name__)

router = APIRouter(prefix="/credits", tags=["credits"])

# --- Dependencies ---
def get_credit_service(supabase: Client = Depends(get_supabase)) -> CreditService:
    """Dependency to provide a CreditService instance."""
    return CreditService(supabase=supabase)

def get_paypal_service() -> PayPalService:
    """Dependency to provide a PayPalService instance."""
    return PayPalService()

def get_user_service(supabase: Client = Depends(get_supabase)) -> UserService:
    """Dependency to provide a UserService instance."""
    return UserService(supabase=supabase)

# --- Credit Purchase Endpoints ---

@router.post("/purchase", response_model=CreditPurchaseResponse, summary="Purchase credits using PayPal")
async def purchase_credits(
    request: CreditPurchaseRequest,
    user: dict = Depends(current_user),
    credit_service: CreditService = Depends(get_credit_service),
    paypal_service: PayPalService = Depends(get_paypal_service),
    user_service: UserService = Depends(get_user_service)
):
    """
    Purchase credits using PayPal payment.
    Creates a PayPal order and returns the approval URL for payment.
    """
    user_id = user.get("id")
    log.info(f"User {user_id} purchasing {request.credits} credits for ${request.price}")
    
    try:
        # Get user profile for PayPal subscription details
        user_profile = user_service.get_user_profile(user_id)
        user_email = user_profile.get("email", "")
        user_name = user_profile.get("full_name", "User")
        
        # Create PayPal order for credit purchase
        base_url = settings.FRONTEND_URL
        return_url = f"{base_url}/payment/success?type=credits"
        cancel_url = f"{base_url}/payment/cancelled?type=credits"
        
        order_id, approval_url = await paypal_service.create_order(
            total_amount=request.price,
            return_url=return_url,
            cancel_url=cancel_url
        )
        
        # Create credit purchase record
        purchase_record = credit_service.create_credit_purchase_record(
            user_id=user_id,
            package_id=request.package_id,
            credits=request.credits,
            price=request.price,
            payment_type="paypal_order",
            payment_id=order_id
        )
        
        log.info(f"Successfully created credit purchase session for user {user_id}. Order ID: {order_id}")
        
        return CreditPurchaseResponse(
            status="pending",
            message="Credit purchase initiated. Please complete payment on PayPal.",
            payment_type="order",
            approval_url=approval_url,
            order_id=order_id
        )
        
    except HTTPException as e:
        log.error(f"HTTP error during credit purchase for user {user_id}: {e.detail}")
        raise e
    except Exception as e:
        log.error(f"Unexpected error during credit purchase for user {user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to initiate credit purchase"
        )

@router.post("/capture-purchase/{order_id}", summary="Capture payment and add credits to user account")
async def capture_credit_purchase(
    order_id: str,
    user: dict = Depends(current_user),
    paypal_service: PayPalService = Depends(get_paypal_service),
    credit_service: CreditService = Depends(get_credit_service)
):
    """
    Capture the PayPal payment and add the purchased credits to the user's account.
    """
    user_id = user.get("id")
    log.info(f"User {user_id} capturing credit purchase for order {order_id}")
    
    try:
        # Capture the PayPal payment
        capture_data = await paypal_service.capture_order(order_id)
        
        if capture_data.get("status") == "COMPLETED":
            # Get the credit purchase record
            purchase_record_res = credit_service.sb.table("credit_purchases").select("*").eq("payment_id", order_id).eq("user_id", user_id).single().execute()
            
            if not purchase_record_res.data:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Credit purchase record not found"
                )
            
            purchase_record = purchase_record_res.data
            credits_to_add = purchase_record["credits"]
            
            # Add credits to user account
            credit_update = credit_service.add_credits(user_id, credits_to_add)
            
            # Update purchase record status
            credit_service.update_credit_purchase_status(order_id, "completed")
            
            log.info(f"Successfully completed credit purchase for user {user_id}. Added {credits_to_add} credits")
            
            return {
                "status": "success",
                "message": f"Payment captured successfully. {credits_to_add} credits added to your account.",
                "credits_added": credits_to_add,
                "new_balance": credit_update["new_balance"]
            }
        else:
            status = capture_data.get("status", "unknown")
            log.warning(f"PayPal payment capture failed with status: {status}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Payment capture failed with status: {status}"
            )
            
    except HTTPException as e:
        log.error(f"HTTP error capturing credit purchase for user {user_id}: {e.detail}")
        raise e
    except Exception as e:
        log.error(f"Unexpected error capturing credit purchase for user {user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to capture credit purchase"
        )

@router.get("/balance", summary="Get current user's credit balance")
def get_credit_balance(
    user: dict = Depends(current_user),
    credit_service: CreditService = Depends(get_credit_service)
):
    """
    Get the current credit balance for the authenticated user.
    """
    user_id = user.get("id")
    log.info(f"Fetching credit balance for user {user_id}")
    
    try:
        credits = credit_service.get_user_credits(user_id)
        
        return {
            "user_id": user_id,
            "credits": credits
        }
        
    except HTTPException as e:
        log.error(f"HTTP error fetching credit balance for user {user_id}: {e.detail}")
        raise e
    except Exception as e:
        log.error(f"Unexpected error fetching credit balance for user {user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch credit balance"
        )
