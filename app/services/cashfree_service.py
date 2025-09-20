# app/services/cashfree_service.py
import requests
from typing import Dict, List, Optional, Tuple
from app.core.logging import get_logger
from app.core.config import settings

log = get_logger(__name__)

USD_CONVERSION_FACTOR = 75

class CashfreeService:
    def __init__(self):
        log.info("CashfreeService: Initializing Cashfree service")
        
        self.client_id = settings.CASHFREE_CLIENT_ID
        self.client_secret = settings.CASHFREE_CLIENT_SECRET
        self.base_url = settings.CASHFREE_BASE_URL
        self.api_version = "2025-01-01"
        
        log.info(f"CashfreeService: Configuration loaded - Base URL: {self.base_url}")
        log.debug(f"CashfreeService: Client ID present: {bool(self.client_id)}")
        log.debug(f"CashfreeService: Client Secret present: {bool(self.client_secret)}")
        
        if not self.client_id or not self.client_secret:
            log.warning("CashfreeService: Cashfree credentials not found in environment variables.")
        else:
            log.info("CashfreeService: Cashfree credentials found, service ready")
    
    def _get_headers(self) -> Dict[str, str]:
        """Get standard headers for Cashfree API requests"""
        return {
            "X-Client-Id": self.client_id,
            "X-Client-Secret": self.client_secret,
            "x-api-version": self.api_version,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
    
    async def create_order(
        self,
        order_amount: float,
        customer_details: Dict[str, str],
        return_url: str,
        order_currency: str = "INR"
    ) -> Tuple[str, str]:
        """
        Create a Cashfree order and return order_id and payment_session_id.
        
        Args:
            order_amount: Total amount for the order
            customer_details: Customer information (customer_id, customer_name, customer_email, customer_phone)
            return_url: URL to redirect after payment completion
            order_currency: Currency code (default: INR)
            
        Returns:
            Tuple of (order_id, payment_session_id)
        """
        log.info(f"CashfreeService: Creating order for amount {order_amount} {order_currency}")
        log.info(f"CashfreeService: Customer: {customer_details.get('customer_name')} ({customer_details.get('customer_email')})")
        log.info(f"CashfreeService: Return URL: {return_url}")
        
        url = f"{self.base_url}/pg/orders"
        headers = self._get_headers()
        
        payload = {
            "order_amount": order_amount * USD_CONVERSION_FACTOR,
            "order_currency": order_currency,
            "customer_details": customer_details,
            "order_meta": {
                "return_url": return_url
            }
        }
        
        self._log_request_details("POST", url, headers, payload)
        
        try:
            response = requests.post(url, headers=headers, json=payload)
            self._log_response_details(response, "Order creation")
            
            response.raise_for_status()
            
            order_data = response.json()
            order_id = order_data["order_id"]
            payment_session_id = order_data["payment_session_id"]
            
            log.info(f"CashfreeService: Successfully created order: {order_id}")
            log.info(f"CashfreeService: Payment session ID: {payment_session_id}")
            
            return order_id, payment_session_id
            
        except requests.RequestException as e:
            self._log_error_details(e, "Order creation")
            raise Exception(f"Cashfree order creation failed: {str(e)}")
        except KeyError as e:
            log.error(f"CashfreeService: Missing required field in order response: {e}")
            log.error(f"CashfreeService: Order response data: {order_data}")
            raise Exception(f"Cashfree order response missing required field: {str(e)}")
        except Exception as e:
            self._log_error_details(e, "Order creation")
            raise Exception(f"Cashfree order creation failed: {str(e)}")
    
    async def verify_order(self, order_id: str) -> Dict:
        """
        Verify the status of a Cashfree order.
        
        Args:
            order_id: The Cashfree order ID to verify
            
        Returns:
            Order details including payment status
        """
        log.info(f"CashfreeService: Verifying order status for {order_id}")
        
        url = f"{self.base_url}/pg/orders/{order_id}"
        headers = self._get_headers()
        
        self._log_request_details("GET", url, headers)
        
        try:
            response = requests.get(url, headers=headers)
            self._log_response_details(response, "Order verification")
            
            response.raise_for_status()
            
            order_data = response.json()
            order_status = order_data.get("order_status", "UNKNOWN")
            
            log.info(f"CashfreeService: Successfully verified order {order_id} - Status: {order_status}")
            
            # Log additional order details
            if "customer_details" in order_data:
                customer = order_data["customer_details"]
                log.info(f"CashfreeService: Order customer: {customer.get('customer_name', 'Unknown')} ({customer.get('customer_email', 'Unknown')})")
            
            if "order_amount" in order_data:
                log.info(f"CashfreeService: Order amount: {order_data.get('order_amount')} {order_data.get('order_currency', 'INR')}")
            
            return order_data
            
        except requests.RequestException as e:
            self._log_error_details(e, "Order verification")
            raise Exception(f"Cashfree order verification failed: {str(e)}")
        except Exception as e:
            self._log_error_details(e, "Order verification")
            raise Exception(f"Cashfree order verification failed: {str(e)}")
    
    def calculate_cart_totals(self, cart_items: List[Dict]) -> float:
        """
        Calculate total amount from cart items for Cashfree payment.
        Since we're only doing one-time payments, we sum all items.
        
        Args:
            cart_items: List of cart items with unit_price and quantity
            
        Returns:
            Total amount for the order
        """
        log.info(f"CashfreeService: Calculating cart totals for {len(cart_items)} items")
        
        total_amount = 0.0
        one_time_items = []
        
        for item in cart_items:
            item_price = item.get("unit_price", 0.0)
            quantity = item.get("quantity", 1)
            book_title = item.get("book", {}).get("generated_title", "Unknown")
            
            item_total = item_price * quantity
            total_amount += item_total
            one_time_items.append({
                "title": book_title, 
                "price": item_price, 
                "quantity": quantity,
                "total": item_total
            })
        
        log.info(f"CashfreeService: Cart calculation complete:")
        log.info(f"CashfreeService: - Total items: {len(one_time_items)}")
        log.info(f"CashfreeService: - Total amount: ${total_amount:.2f}")
        
        if one_time_items:
            log.debug(f"CashfreeService: Items: {one_time_items}")
        
        return total_amount
    
    def log_service_health(self) -> Dict[str, any]:
        """Log and return service health information"""
        health_info = {
            "service": "CashfreeService",
            "configured": bool(self.client_id and self.client_secret),
            "base_url": self.base_url,
            "api_version": self.api_version
        }
        
        log.info(f"CashfreeService: Health check - {health_info}")
        return health_info
    
    def _log_request_details(self, method: str, url: str, headers: Dict, payload: Dict = None) -> None:
        """Log detailed request information for debugging"""
        log.debug(f"CashfreeService: {method} request to {url}")
        log.debug(f"CashfreeService: Request headers: {dict(headers)}")
        if payload:
            log.debug(f"CashfreeService: Request payload: {payload}")
    
    def _log_response_details(self, response: requests.Response, operation: str) -> None:
        """Log detailed response information for debugging"""
        log.debug(f"CashfreeService: {operation} response status: {response.status_code}")
        log.debug(f"CashfreeService: {operation} response headers: {dict(response.headers)}")
        
        try:
            response_data = response.json()
            log.debug(f"CashfreeService: {operation} response data: {response_data}")
        except Exception as e:
            log.debug(f"CashfreeService: Could not parse {operation} response as JSON: {e}")
            log.debug(f"CashfreeService: {operation} response text: {response.text[:500]}...")
    
    def _log_error_details(self, error: Exception, operation: str) -> None:
        """Log detailed error information for debugging"""
        log.error(f"CashfreeService: {operation} failed with error: {type(error).__name__}: {str(error)}")
        
        if hasattr(error, 'response') and error.response is not None:
            log.error(f"CashfreeService: {operation} error response status: {error.response.status_code}")
            log.error(f"CashfreeService: {operation} error response headers: {dict(error.response.headers)}")
            try:
                error_data = error.response.json()
                log.error(f"CashfreeService: {operation} error response data: {error_data}")
            except Exception:
                log.error(f"CashfreeService: {operation} error response text: {error.response.text[:500]}...")
        
        log.error(f"CashfreeService: {operation} error details: {error}", exc_info=True)