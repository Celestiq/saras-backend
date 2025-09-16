# app/services/paypal_service.py
import os
import requests
from typing import Dict, List, Optional, Tuple
from app.core.logging import get_logger

log = get_logger(__name__)

class PayPalService:
    def __init__(self):
        log.info("PayPalService: Initializing PayPal service")
        
        self.client_id = os.getenv("PAYPAL_CLIENT_ID")
        self.client_secret = os.getenv("PAYPAL_CLIENT_SECRET")
        self.base_url = os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com")
        self.product_id = os.getenv("PAYPAL_PRODUCT_ID")
        self.access_token = None
        self.token_expires_at = None
        
        log.info(f"PayPalService: Configuration loaded - Base URL: {self.base_url}")
        log.debug(f"PayPalService: Product ID present: {bool(self.product_id)}")
        
        if not self.client_id or not self.client_secret or not self.product_id:
            log.warning("PayPalService: PayPal credentials or Product ID not found in environment variables.")
        else:
            log.info("PayPalService: PayPal credentials and Product ID found, service ready")
    
    async def get_access_token(self) -> str:
        """Obtain an access token from PayPal API"""
        log.info("PayPalService: Starting access token retrieval")
        
        if self.access_token and self.token_expires_at and self.token_expires_at > self._current_timestamp():
            log.info("PayPalService: Using cached access token (still valid)")
            return self.access_token
            
        log.info("PayPalService: Access token expired or not available, requesting new token")
        url = f"{self.base_url}/v1/oauth2/token"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {self._get_basic_auth()}"
        }
        data = "grant_type=client_credentials"
        
        self._log_request_details("POST", url, headers, {"data": data})
        
        try:
            response = requests.post(url, headers=headers, data=data)
            self._log_response_details(response, "Token")
            
            response.raise_for_status()
            
            token_data = response.json()
            self.access_token = token_data["access_token"]
            expires_in = token_data.get("expires_in", 32400)
            self.token_expires_at = self._current_timestamp() + expires_in - 60  # 1 minute buffer
            
            log.info(f"PayPalService: Successfully obtained new access token, expires in {expires_in} seconds")
            log.debug(f"PayPalService: Token expires at timestamp: {self.token_expires_at}")
            return self.access_token
            
        except requests.RequestException as e:
            self._log_error_details(e, "Token retrieval")
            raise Exception(f"PayPal authentication failed: {str(e)}")
        except KeyError as e:
            log.error(f"PayPalService: Missing required field in token response: {e}")
            log.error(f"PayPalService: Token response data: {token_data}")
            raise Exception(f"PayPal token response missing required field: {str(e)}")
        except Exception as e:
            self._log_error_details(e, "Token retrieval")
            raise Exception(f"PayPal authentication failed: {str(e)}")
    
    def _get_basic_auth(self) -> str:
        """Generate Basic Auth header for PayPal API"""
        import base64
        credentials = f"{self.client_id}:{self.client_secret}"
        return base64.b64encode(credentials.encode()).decode()
    
    def _current_timestamp(self) -> int:
        """Get current timestamp in seconds"""
        import time
        return int(time.time())
    
    
    async def create_billing_plan(
        self, 
        subscription_price: float, 
        setup_fee: float,
        currency: str = "USD"
    ) -> str:
        """Create a billing plan using the static product_id."""
        log.info(f"PayPalService: Creating billing plan for static product {self.product_id}")
        log.info(f"PayPalService: Subscription price: {subscription_price:.2f} {currency}, Setup fee: {setup_fee:.2f} {currency}")
        
        token = await self.get_access_token()
        url = f"{self.base_url}/v1/billing/plans"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "product_id": self.product_id,
            "name": "Premium Content Subscription with One-time Items",
            "description": "Monthly subscription and one-time purchase items.",
            "status": "ACTIVE",
            "billing_cycles": [
                {
                    "frequency": {
                        "interval_unit": "MONTH",
                        "interval_count": 1
                    },
                    "tenure_type": "REGULAR",
                    "sequence": 1,
                    "total_cycles": 0,  # 0 for infinite cycles
                    "pricing_scheme": {
                        "fixed_price": {
                            "value": f"{subscription_price:.2f}",
                            "currency_code": currency
                        }
                    }
                }
            ],
            "payment_preferences": {
                "auto_bill_outstanding": True,
                "setup_fee": {
                    "value": f"{setup_fee:.2f}",
                    "currency_code": currency
                },
                "setup_fee_failure_action": "CANCEL",
                "payment_failure_threshold": 3
            }
        }
        
        self._log_request_details("POST", url, headers, payload)
        
        try:
            response = requests.post(url, headers=headers, json=payload)
            self._log_response_details(response, "Billing plan creation")
            
            response.raise_for_status()
            
            plan_data = response.json()
            plan_id = plan_data["id"]
            log.info(f"PayPalService: Successfully created PayPal billing plan: {plan_id}")
            return plan_id
            
        except requests.RequestException as e:
            self._log_error_details(e, "Billing plan creation")
            raise Exception(f"PayPal billing plan creation failed: {str(e)}")
        except KeyError as e:
            log.error(f"PayPalService: Missing required field in billing plan response: {e}")
            log.error(f"PayPalService: Billing plan response data: {plan_data}")
            raise Exception(f"PayPal billing plan response missing required field: {str(e)}")
        except Exception as e:
            self._log_error_details(e, "Billing plan creation")
            raise Exception(f"PayPal billing plan creation failed: {str(e)}")
    
    async def create_subscription(
        self, 
        plan_id: str, 
        subscriber_name: str, 
        subscriber_email: str,
        return_url: str,
        cancel_url: str
    ) -> Tuple[str, str]:
        """Create a subscription and return subscription ID and approval URL"""
        log.info(f"PayPalService: Creating subscription for plan {plan_id}")
        log.info(f"PayPalService: Subscriber: {subscriber_name} ({subscriber_email})")
        log.info(f"PayPalService: Return URL: {return_url}")
        log.info(f"PayPalService: Cancel URL: {cancel_url}")
        
        token = await self.get_access_token()
        url = f"{self.base_url}/v1/billing/subscriptions"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        # Parse subscriber name
        name_parts = subscriber_name.split(" ", 1)
        given_name = name_parts[0] if name_parts else "User"
        surname = name_parts[1] if len(name_parts) > 1 else ""
        
        log.debug(f"PayPalService: Parsed subscriber name - Given: '{given_name}', Surname: '{surname}'")
        
        payload = {
            "plan_id": plan_id,
            "start_time": self._get_start_time(),
            "subscriber": {
                "name": {
                    "given_name": given_name,
                    "surname": surname
                },
                "email_address": subscriber_email
            },
            "application_context": {
                "brand_name": "Saras",
                "locale": "en-US",
                "shipping_preference": "SET_PROVIDED_ADDRESS",
                "user_action": "SUBSCRIBE_NOW",
                "payment_method": {
                    "payer_selected": "PAYPAL",
                    "payee_preferred": "IMMEDIATE_PAYMENT_REQUIRED"
                },
                "return_url": return_url,
                "cancel_url": cancel_url
            }
        }
        
        self._log_request_details("POST", url, headers, payload)
        
        try:
            response = requests.post(url, headers=headers, json=payload)
            self._log_response_details(response, "Subscription creation")
            
            response.raise_for_status()
            
            subscription_data = response.json()
            subscription_id = subscription_data["id"]
            log.info(f"PayPalService: Successfully created PayPal subscription: {subscription_id}")
            
            # Find the approval URL
            approval_url = None
            links = subscription_data.get("links", [])
            log.debug(f"PayPalService: Found {len(links)} links in subscription response")
            
            for link in links:
                log.debug(f"PayPalService: Link - rel: {link.get('rel')}, href: {link.get('href')}")
                if link.get("rel") == "approve":
                    approval_url = link.get("href")
                    break
            
            if not approval_url:
                log.error(f"PayPalService: No approval URL found in subscription response")
                log.error(f"PayPalService: Available links: {links}")
                raise Exception("No approval URL found in PayPal response")
            
            log.info(f"PayPalService: Found approval URL: {approval_url}")
            return subscription_id, approval_url
            
        except requests.RequestException as e:
            self._log_error_details(e, "Subscription creation")
            raise Exception(f"PayPal subscription creation failed: {str(e)}")
        except KeyError as e:
            log.error(f"PayPalService: Missing required field in subscription response: {e}")
            log.error(f"PayPalService: Subscription response data: {subscription_data}")
            raise Exception(f"PayPal subscription response missing required field: {str(e)}")
        except Exception as e:
            self._log_error_details(e, "Subscription creation")
            raise Exception(f"PayPal subscription creation failed: {str(e)}")
    
    async def verify_subscription(self, subscription_id: str) -> Dict:
        """Verify subscription status after user approval"""
        log.info(f"PayPalService: Verifying subscription status for {subscription_id}")
        
        token = await self.get_access_token()
        url = f"{self.base_url}/v1/billing/subscriptions/{subscription_id}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        self._log_request_details("GET", url, headers)
        
        try:
            response = requests.get(url, headers=headers)
            self._log_response_details(response, "Subscription verification")
            
            response.raise_for_status()
            
            subscription_data = response.json()
            status = subscription_data.get("status", "UNKNOWN")
            log.info(f"PayPalService: Successfully verified PayPal subscription {subscription_id} - Status: {status}")
            
            # Log additional subscription details
            if "subscriber" in subscription_data:
                subscriber = subscription_data["subscriber"]
                log.info(f"PayPalService: Subscription subscriber: {subscriber.get('email_address', 'Unknown')}")
            
            if "billing_info" in subscription_data:
                billing_info = subscription_data["billing_info"]
                log.info(f"PayPalService: Next billing date: {billing_info.get('next_billing_time', 'Unknown')}")
                log.info(f"PayPalService: Outstanding balance: {billing_info.get('outstanding_balance', {}).get('value', 'Unknown')}")
            
            return subscription_data
            
        except requests.RequestException as e:
            self._log_error_details(e, "Subscription verification")
            raise Exception(f"PayPal subscription verification failed: {str(e)}")
        except Exception as e:
            self._log_error_details(e, "Subscription verification")
            raise Exception(f"PayPal subscription verification failed: {str(e)}")
    
    def _get_start_time(self) -> str:
        """Get start time for subscription (7:00 AM next day)"""
        from datetime import datetime, timezone, timedelta
        
        # Get current time in UTC
        now = datetime.now(timezone.utc)
        
        # Calculate next day at 7:00 AM UTC
        next_day = now + timedelta(days=1)
        start_time = next_day.replace(hour=7, minute=0, second=0, microsecond=0)
        
        log.info(f"PayPalService: Setting subscription start time to {start_time.isoformat()} (7:00 AM UTC next day)")
        log.debug(f"PayPalService: Current time: {now.isoformat()}")
        log.debug(f"PayPalService: Start time: {start_time.isoformat()}")
        
        return start_time.isoformat()
    
    def calculate_cart_totals(self, cart_items: List[Dict]) -> Tuple[float, float]:
        """Calculate subscription total and setup fee from cart items"""
        log.info(f"PayPalService: Calculating cart totals for {len(cart_items)} items")
        
        subscription_total = 0.0
        setup_fee = 0.0
        subscription_items = []
        one_time_items = []
        
        for item in cart_items:
            item_price = item.get("unit_price", 0.0)
            is_subscription = item.get("subscription", False)
            book_title = item.get("book", {}).get("generated_title", "Unknown")
            
            log.debug(f"PayPalService: Processing item - Title: '{book_title}', Price: ${item_price:.2f}, Subscription: {is_subscription}")
            
            if is_subscription:
                subscription_total += item_price
                subscription_items.append({"title": book_title, "price": item_price})
            else:
                setup_fee += item_price
                one_time_items.append({"title": book_title, "price": item_price})
        
        log.info(f"PayPalService: Cart calculation complete:")
        log.info(f"PayPalService: - Subscription items ({len(subscription_items)}): ${subscription_total:.2f}")
        log.info(f"PayPalService: - One-time items ({len(one_time_items)}): ${setup_fee:.2f}")
        log.info(f"PayPalService: - Total cart value: ${subscription_total + setup_fee:.2f}")
        
        if subscription_items:
            log.debug(f"PayPalService: Subscription items: {subscription_items}")
        if one_time_items:
            log.debug(f"PayPalService: One-time items: {one_time_items}")
        
        log.info("Returning Subscription total as 0.0 and Setup fee as total cart value for PayPal processing")
        return 0.0, setup_fee + subscription_total
    
    async def create_order(
        self,
        total_amount: float,
        return_url: str,
        cancel_url: str,
        currency: str = "USD"
    ) -> Tuple[str, str]:
        """Create a one-time payment order using the Checkout API."""
        log.info(f"PayPalService: Creating one-time order for {total_amount:.2f} {currency}")
        token = await self.get_access_token()
        url = f"{self.base_url}/v2/checkout/orders"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        payload = {
            "intent": "CAPTURE",
            "purchase_units": [{"amount": {"currency_code": currency, "value": f"{total_amount:.2f}"}}],
            "application_context": {
                "return_url": return_url,
                "cancel_url": cancel_url,
                "brand_name": "Saras",
                "user_action": "PAY_NOW"
            }
        }
        self._log_request_details("POST", url, headers, payload)
        try:
            response = requests.post(url, headers=headers, json=payload)
            self._log_response_details(response, "Order creation")
            response.raise_for_status()
            order_data = response.json()
            order_id = order_data["id"]
            approval_url = next((link["href"] for link in order_data["links"] if link["rel"] == "approve"), None)
            log.debug(f"PayPalService: Approval URL: {approval_url}")
            if not approval_url:
                raise Exception("No approval URL found in order response")
            log.info(f"PayPalService: Successfully created one-time order: {order_id}")
            return order_id, approval_url
        except requests.RequestException as e:
            self._log_error_details(e, "Order creation")
            raise Exception(f"PayPal order creation failed: {str(e)}")

    async def capture_order(self, order_id: str) -> Dict:
        """Capture the payment for a one-time order."""
        log.info(f"PayPalService: Capturing payment for order {order_id}")
        token = await self.get_access_token()
        url = f"{self.base_url}/v2/checkout/orders/{order_id}/capture"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self._log_request_details("POST", url, headers)
        try:
            response = requests.post(url, headers=headers)
            self._log_response_details(response, "Order capture")
            response.raise_for_status()
            capture_data = response.json()
            log.info(f"PayPalService: Successfully captured payment for order {order_id}")
            return capture_data
        except requests.RequestException as e:
            self._log_error_details(e, "Order capture")
            raise Exception(f"PayPal order capture failed: {str(e)}")

    async def cancel_subscription(self, subscription_id: str, reason: str = "Cancelled by user") -> bool:
        """Cancel a subscription via the API."""
        log.info(f"PayPalService: Cancelling subscription {subscription_id} with reason: '{reason}'")
        token = await self.get_access_token()
        url = f"{self.base_url}/v1/billing/subscriptions/{subscription_id}/cancel"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        payload = {"reason": reason}
        self._log_request_details("POST", url, headers, payload)
        try:
            response = requests.post(url, headers=headers, json=payload)
            self._log_response_details(response, "Subscription cancellation")
            response.raise_for_status()
            return response.status_code == 204
        except requests.RequestException as e:
            self._log_error_details(e, "Subscription cancellation")
            raise Exception(f"PayPal subscription cancellation failed: {str(e)}")
    
    def log_service_health(self) -> Dict[str, any]:
        """Log and return service health information"""
        health_info = {
            "service": "PayPalService",
            "configured": bool(self.client_id and self.client_secret),
            "base_url": self.base_url,
            "has_token": bool(self.access_token),
            "token_expires_at": self.token_expires_at,
            "token_valid": bool(self.access_token and self.token_expires_at and self.token_expires_at > self._current_timestamp())
        }
        
        log.info(f"PayPalService: Health check - {health_info}")
        return health_info
    
    def _log_request_details(self, method: str, url: str, headers: Dict, payload: Dict = None) -> None:
        """Log detailed request information for debugging"""
        log.debug(f"PayPalService: {method} request to {url}")
        log.debug(f"PayPalService: Request headers: {dict(headers)}")
        if payload:
            log.debug(f"PayPalService: Request payload: {payload}")
    
    def _log_response_details(self, response: requests.Response, operation: str) -> None:
        """Log detailed response information for debugging"""
        log.debug(f"PayPalService: {operation} response status: {response.status_code}")
        log.debug(f"PayPalService: {operation} response headers: {dict(response.headers)}")
        
        try:
            response_data = response.json()
            log.debug(f"PayPalService: {operation} response data: {response_data}")
        except Exception as e:
            log.debug(f"PayPalService: Could not parse {operation} response as JSON: {e}")
            log.debug(f"PayPalService: {operation} response text: {response.text[:500]}...")
    
    def _log_error_details(self, error: Exception, operation: str) -> None:
        """Log detailed error information for debugging"""
        log.error(f"PayPalService: {operation} failed with error: {type(error).__name__}: {str(error)}")
        
        if hasattr(error, 'response') and error.response is not None:
            log.error(f"PayPalService: {operation} error response status: {error.response.status_code}")
            log.error(f"PayPalService: {operation} error response headers: {dict(error.response.headers)}")
            try:
                error_data = error.response.json()
                log.error(f"PayPalService: {operation} error response data: {error_data}")
            except Exception:
                log.error(f"PayPalService: {operation} error response text: {error.response.text[:500]}...")
        
        log.error(f"PayPalService: {operation} error details: {error}", exc_info=True)
