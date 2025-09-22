# app/services/cloud_tasks_service.py
import json
import os
from typing import Dict, Any, Optional
from google.cloud import tasks_v2
from google.protobuf import timestamp_pb2
from datetime import datetime, timezone, timedelta
from google.oauth2 import service_account

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

class CloudTasksService:
    """
    Service for managing Google Cloud Tasks for background job processing.
    """
    
    def __init__(self):
        """Initialize the Cloud Tasks client with service account authentication."""
        # Load service account credentials
        try:
            if os.path.exists(settings.GCP_SERVICE_ACCOUNT_KEY_PATH):
                credentials = service_account.Credentials.from_service_account_file(
                    settings.GCP_SERVICE_ACCOUNT_KEY_PATH
                )
                self.client = tasks_v2.CloudTasksClient(credentials=credentials)
                log.info(f"Cloud Tasks client initialized with service account: {settings.GCP_SERVICE_ACCOUNT_KEY_PATH}")
            else:
                # Fallback to default credentials (useful for cloud deployment)
                self.client = tasks_v2.CloudTasksClient()
                log.warning(f"Service account file not found at {settings.GCP_SERVICE_ACCOUNT_KEY_PATH}, using default credentials")
        except Exception as e:
            log.error(f"Failed to initialize Cloud Tasks client with service account: {e}")
            # Fallback to default credentials
            self.client = tasks_v2.CloudTasksClient()
            log.warning("Using default Cloud Tasks client credentials")
        
        self.project_id = settings.GCP_PROJECT_ID
        self.location = settings.GCP_LOCATION
        self.queue_name = settings.GCP_QUEUE_NAME
        self.parent = self.client.queue_path(self.project_id, self.location, self.queue_name)
        log.info(f"Cloud Tasks service initialized with queue: {self.parent}")
    
    def create_chapter_generation_task(
        self, 
        order_id: str, 
        delay_seconds: int = 0
    ) -> str:
        """
        Create a Cloud Task for chapter generation.
        
        Args:
            order_id: The order ID to process
            delay_seconds: Optional delay before task execution (default: 0)
            
        Returns:
            str: The task name/ID
        """
        try:
            # Prepare the task payload - only include order_id
            task_payload = {
                "order_id": order_id
            }
            
            # Create the task
            task = {
                "http_request": {
                    "http_method": tasks_v2.HttpMethod.POST,
                    "url": f"{settings.BACKEND_URL}/tasks/generate-chapters",
                    "headers": {
                        "Content-Type": "application/json"
                    },
                    "body": json.dumps(task_payload).encode("utf-8"),
                },
            }
            
            # Add OIDC token for service account authentication
            service_account_email = settings.CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL or "task-invoker@lateral-berm-471911-k2.iam.gserviceaccount.com"
            task["http_request"]["oidc_token"] = {
                "service_account_email": service_account_email,
                "audience": f"{settings.BACKEND_URL}/tasks/generate-chapters"
            }
            
            # Add delay if specified
            if delay_seconds > 0:
                timestamp = timestamp_pb2.Timestamp()
                timestamp.FromDatetime(
                    datetime.now(timezone.utc).replace(
                        microsecond=0
                    ) + timedelta(seconds=delay_seconds)
                )
                task["schedule_time"] = timestamp
            
            # Submit the task
            response = self.client.create_task(
                request={"parent": self.parent, "task": task}
            )
            
            task_name = response.name
            log.info(f"Created Cloud Task for chapter generation: {task_name} for order_id: {order_id}")
            
            return task_name
            
        except Exception as e:
            log.error(f"Failed to create Cloud Task for order_id: {order_id}. Error: {e}", exc_info=True)
            raise Exception(f"Failed to create chapter generation task: {str(e)}")
    
    def create_retry_task(
        self,
        order_id: str,
        retry_count: int,
        delay_seconds: int = 300  # 5 minutes default delay
    ) -> str:
        """
        Create a retry task for failed chapter generation.
        
        Args:
            order_id: The order ID to retry
            retry_count: Current retry attempt number
            delay_seconds: Delay before retry (default: 5 minutes)
            
        Returns:
            str: The retry task name/ID
        """
        try:
            # Create retry payload
            retry_payload = {
                "order_id": order_id,
                "retry_count": retry_count
            }
            
            # Create the retry task
            task = {
                "http_request": {
                    "http_method": tasks_v2.HttpMethod.POST,
                    "url": f"{settings.BACKEND_URL}/tasks/generate-chapters",
                    "headers": {
                        "Content-Type": "application/json"
                    },
                    "body": json.dumps(retry_payload).encode("utf-8"),
                },
            }
            
            # Add OIDC token for service account authentication
            service_account_email = settings.CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL or "task-invoker@lateral-berm-471911-k2.iam.gserviceaccount.com"
            task["http_request"]["oidc_token"] = {
                "service_account_email": service_account_email,
                "audience": f"{settings.BACKEND_URL}/tasks/generate-chapters"
            }
            
            # Add delay
            timestamp = timestamp_pb2.Timestamp()
            timestamp.FromDatetime(
                datetime.now(timezone.utc).replace(
                    microsecond=0
                ) + timedelta(seconds=delay_seconds)
            )
            task["schedule_time"] = timestamp
            
            # Submit the retry task
            response = self.client.create_task(
                request={"parent": self.parent, "task": task}
            )
            
            task_name = response.name
            log.info(f"Created retry Cloud Task (attempt {retry_count}): {task_name} for order_id: {order_id}")
            
            return task_name
            
        except Exception as e:
            log.error(f"Failed to create retry Cloud Task for order_id: {order_id}, retry {retry_count}. Error: {e}", exc_info=True)
            raise Exception(f"Failed to create retry task: {str(e)}")
    
    def create_individual_chapter_task(
        self, 
        chapter_id: str,
        order_id: str,
        book_id: str,
        user_id: str,
        module_index: int,
        topic_index: int,
        idx: int,
        delay_seconds: int = 0
    ) -> str:
        """
        Create a Cloud Task for individual chapter generation.
        
        Args:
            chapter_id: The chapter ID to generate content for
            order_id: The order ID this chapter belongs to
            book_id: The book ID this chapter belongs to
            user_id: The user ID who owns this chapter
            module_index: Module index in the roadmap
            topic_index: Topic index within the module
            idx: Chapter index/number
            delay_seconds: Optional delay before task execution (default: 0)
            
        Returns:
            str: The task name/ID
        """
        try:
            # Prepare the task payload for individual chapter generation
            task_payload = {
                "chapter_id": chapter_id,
                "order_id": order_id,
                "book_id": book_id,
                "user_id": user_id,
                "module_index": module_index,
                "topic_index": topic_index,
                "idx": idx
            }
            
            # Create the task
            task = {
                "http_request": {
                    "http_method": tasks_v2.HttpMethod.POST,
                    "url": f"{settings.BACKEND_URL}/tasks/generate-single-chapter",
                    "headers": {
                        "Content-Type": "application/json"
                    },
                    "body": json.dumps(task_payload).encode("utf-8"),
                },
            }
            
            # Add OIDC token for service account authentication
            service_account_email = settings.CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL or "task-invoker@lateral-berm-471911-k2.iam.gserviceaccount.com"
            task["http_request"]["oidc_token"] = {
                "service_account_email": service_account_email,
                "audience": f"{settings.BACKEND_URL}/tasks/generate-single-chapter"
            }
            
            # Add delay if specified
            if delay_seconds > 0:
                timestamp = timestamp_pb2.Timestamp()
                timestamp.FromDatetime(
                    datetime.now(timezone.utc).replace(
                        microsecond=0
                    ) + timedelta(seconds=delay_seconds)
                )
                task["schedule_time"] = timestamp
            
            # Submit the task
            response = self.client.create_task(
                request={"parent": self.parent, "task": task}
            )
            
            task_name = response.name
            log.info(f"Created Cloud Task for individual chapter generation: {task_name} for chapter_id: {chapter_id}")
            
            return task_name
            
        except Exception as e:
            log.error(f"Failed to create Cloud Task for chapter_id: {chapter_id}. Error: {e}", exc_info=True)
            raise Exception(f"Failed to create individual chapter generation task: {str(e)}")
    
    def create_completion_monitoring_task(
        self,
        order_id: str,
        delay_seconds: int = 60  # 1 minute default delay
    ) -> str:
        """
        Create a Cloud Task to monitor chapter completion and trigger final processing.
        
        Args:
            order_id: The order ID to monitor
            delay_seconds: Delay before checking completion (default: 1 minute)
            
        Returns:
            str: The task name/ID
        """
        try:
            # Prepare the task payload for completion monitoring
            task_payload = {
                "order_id": order_id
            }
            
            # Create the task
            task = {
                "http_request": {
                    "http_method": tasks_v2.HttpMethod.POST,
                    "url": f"{settings.BACKEND_URL}/tasks/monitor-completion",
                    "headers": {
                        "Content-Type": "application/json"
                    },
                    "body": json.dumps(task_payload).encode("utf-8"),
                },
            }
            
            # Add OIDC token for service account authentication
            service_account_email = settings.CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL or "task-invoker@lateral-berm-471911-k2.iam.gserviceaccount.com"
            task["http_request"]["oidc_token"] = {
                "service_account_email": service_account_email,
                "audience": f"{settings.BACKEND_URL}/tasks/monitor-completion"
            }
            
            # Add delay
            timestamp = timestamp_pb2.Timestamp()
            timestamp.FromDatetime(
                datetime.now(timezone.utc).replace(
                    microsecond=0
                ) + timedelta(seconds=delay_seconds)
            )
            task["schedule_time"] = timestamp
            
            # Submit the task
            response = self.client.create_task(
                request={"parent": self.parent, "task": task}
            )
            
            task_name = response.name
            log.info(f"Created completion monitoring Cloud Task: {task_name} for order_id: {order_id}")
            
            return task_name
            
        except Exception as e:
            log.error(f"Failed to create completion monitoring Cloud Task for order_id: {order_id}. Error: {e}", exc_info=True)
            raise Exception(f"Failed to create completion monitoring task: {str(e)}")
    
    def get_queue_info(self) -> Dict[str, Any]:
        """
        Get information about the Cloud Tasks queue.
        
        Returns:
            Dict containing queue information
        """
        try:
            queue = self.client.get_queue(name=self.parent)
            return {
                "name": queue.name,
                "state": queue.state.name,
                "tasks_count": getattr(queue, 'tasks_count', 'unknown'),
                "retry_config": {
                    "max_attempts": queue.retry_config.max_attempts,
                    "max_retry_duration": str(queue.retry_config.max_retry_duration),
                }
            }
        except Exception as e:
            log.error(f"Failed to get queue info: {e}", exc_info=True)
            return {"error": str(e)}
