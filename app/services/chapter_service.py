# app/services/chapter_service.py
from __future__ import annotations
import json
import uuid
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from openai import OpenAI
from supabase import Client

from app.core.config import settings
from app.utils.llm import get_response
from app.utils.helper import remove_citations
from app.prompts.prompts import TOPIC_CONTENT_SYSTEM, TOPIC_CONTENT_USER
from app.core.logging import get_logger

# Instantiate the logger for this specific module
log = get_logger(__name__)

class ChapterService:
    def __init__(self, supabase: Client, openai_client: Optional[OpenAI] = None):
        self.sb = supabase
        self.client = openai_client or OpenAI()
        self.books_bucket = getattr(settings, "APP_BUCKET_BOOKS", "books")
        self.chapters_bucket = getattr(settings, "APP_BUCKET_CHAPTERS", "chapters")

    # ----- Private helpers ---------------------------------------------------
    def _insert_llm_run(self, payload: Dict[str, Any]) -> None:
        self.sb.table("llm_runs").insert(payload).execute()

    def _upload_md(self, bucket: str, path: str, content: str) -> str:
        data = content.encode("utf-8")
        log.info(f"Uploading markdown to storage at {bucket}/{path}")
        try:
            self.sb.storage.from_(bucket).upload(path=path, file=data, file_options={"content-type": "text/markdown"})
        except Exception:
            log.warning(f"File already exists at {bucket}/{path}. Overwriting.")
            self.sb.storage.from_(bucket).update(path=path, file=data, file_options={"content-type": "text/markdown"})
        return f"{bucket}/{path}"

    def _download_json(self, bucket: str, path: str) -> dict:
        log.info(f"Downloading JSON from storage at {bucket}/{path}")
        raw = self.sb.storage.from_(bucket).download(path)
        return json.loads(raw.decode("utf-8"))

    @staticmethod
    def _sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    # ----- Book status management --------------------------------------------
    def update_book_status(self, book_id: str, status: str) -> None:
        """Updates the status of a book in the database."""
        log.info(f"Updating book_id: {book_id} status to: '{status}'")
        try:
            self.sb.table("books").update({"status": status}).eq("id", book_id).execute()
            log.info(f"Successfully updated book_id: {book_id} status to '{status}'")
        except Exception as e:
            log.error(f"Failed to update book status for book_id: {book_id}. Error: {e}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update book status: {e}"
            )

    def update_books_status_for_order(self, order_items: List[dict], status: str) -> None:
        """Updates the status of all books in an order."""
        log.info(f"Updating status to '{status}' for {len(order_items)} books in order")
        for item in order_items:
            book_id = item.get("book_id")
            if book_id:
                try:
                    self.update_book_status(book_id, status)
                except Exception as e:
                    log.error(f"Failed to update status for book_id: {book_id}. Error: {e}", exc_info=True)
                    # Continue with other books even if one fails
            else:
                log.warning(f"Skipping item with no book_id: {item}")

    # ----- Core generation logic --------------------------------------------
    def generate_content_for_topic(self, learning_path: dict, module_index: int, topic_index: int) -> str:
        topic_title = learning_path.get("modules", [])[module_index].get("topics", [])[topic_index].get("title", "N/A")
        log.info(f"Generating LLM content for topic: '{topic_title}'")
        
        module = learning_path["modules"][module_index]
        subject = learning_path.get("subject")
        topic = module["topics"][topic_index]
        user_prompt = TOPIC_CONTENT_USER.format(
            subject=subject,
            module_number=module_index + 1,
            module_title=module['module_title'],
            module_goal=module['module_goal'],
            all_topics=",".join([f"- {t['title']}: {t['context']}" for t in module['topics']]),
            current_topic=topic.get("title"),
            current_context=topic.get("context")
        )

        response = get_response(
            client=self.client,
            system_prompt=TOPIC_CONTENT_SYSTEM,
            user_prompt=user_prompt,
            model=settings.CONTENT_MODEL,
            verbosity="medium",
            effort=settings.CONTENT_EFFORT,
            web_search=settings.WEB_SEARCH,
        )
        return remove_citations(response)

    def _ensure_chapter_row(self, book_id: str, idx: int, title: Optional[str]) -> dict:
        log.info(f"Ensuring chapter row exists for book_id: {book_id}, index: {idx}")
        response = self.sb.table("chapters").select("*").eq("book_id", book_id).eq("idx", idx).execute()
        if response.data:
            log.info(f"Found existing chapter row for index {idx}.")
            row = response.data[0]
            if title and not row.get("title"):
                self.sb.table("chapters").update({"title": title}).eq("id", row["id"]).execute()
                row["title"] = title
            return row
        
        log.info(f"No chapter row found for index {idx}. Creating new one.")
        row = {"id": str(uuid.uuid4()), "book_id": book_id, "idx": idx, "title": title}
        created = self.sb.table("chapters").insert(row).execute().data[0]
        return created

    def _save_chapter_content(self, chapter_id: str, book_id: str, idx: int, content_md: str) -> dict:
        log.info(f"Saving content for chapter_id: {chapter_id} (index: {idx})")
        path_in_bucket = f"books/{book_id}/chapters/{idx}.md"
        storage_url = self._upload_md(self.chapters_bucket, path_in_bucket, content_md)
        data_bytes = content_md.encode("utf-8")
        sha = self._sha256(data_bytes)
        
        updated = self.sb.table("chapters").update({
            "content_path": storage_url,
            "content_sha256": sha,
            "content_bytes": len(data_bytes),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", chapter_id).execute()
        log.info(f"Successfully saved content for chapter_id: {chapter_id}")
        return updated.data

    def _load_book_and_roadmap(self, book_id: str) -> tuple[dict, dict]:
        log.info(f"Loading book and roadmap for book_id: {book_id}")
        book_res = self.sb.table("books").select("*").eq("id", book_id).maybe_single().execute()
        if not book_res or not book_res.data:
            log.error(f"Book not found for book_id: {book_id}")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Book not found")
        book = book_res.data
        
        content_url: str = book.get("content_url")
        if not content_url or "/" not in content_url:
            log.error(f"Book {book_id} has a missing or malformed content_url.")
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Book content_url is missing or malformed")
        
        bucket, *path_parts = content_url.split("/")
        path = "/".join(path_parts)
        roadmap = self._download_json(bucket, path)
        return book, roadmap

    # Public API --------------------------------------------------------------
    def generate_chapter(self, *, user_id: str, wish_id: Optional[str], book_id: str, idx: int, module_index: int, topic_index: int) -> dict:
        log.info(f"Starting chapter generation for book_id: {book_id}, chapter index: {idx}")
        book, roadmap = self._load_book_and_roadmap(book_id)
        started = datetime.now(timezone.utc)
        err: Optional[str] = None
        payload: Optional[Any] = None

        title = roadmap["modules"][module_index]["topics"][topic_index].get("title")
        row = self._ensure_chapter_row(book_id, idx, title)

        try:
            md = self.generate_content_for_topic(roadmap, module_index, topic_index)
            saved = self._save_chapter_content(row["id"], book_id, idx, md)
            payload = {"chapter": saved}
            log.info(f"Successfully generated and saved chapter index {idx} for book {book_id}")
        except Exception as e:
            log.error(f"An error occurred during content generation for chapter {idx} of book {book_id}: {e}", exc_info=True)
            err = str(e)

        latency_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)

        self._insert_llm_run({
            "id": str(uuid.uuid4()), "user_id": user_id, "wish_id": wish_id,
            "book_id": book_id, "chapter_id": row["id"], "prompt_name": "chapter_content",
            "prompt_version": 1, "provider": "openai", "model": settings.CONTENT_MODEL,
            "latency_ms": latency_ms, "status_code": 200 if err is None else 500, "error": err,
            "request_json": {"module_index": module_index, "topic_index": topic_index, "idx": idx},
            "response_json": payload if err is None else {"error": err},
        })

        if err:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=err)
        return payload

    def generate_all_chapters(self, *, user_id: str, wish_id: Optional[str], book_id: str) -> dict:
        log.info(f"Starting 'generate_all_chapters' process for book_id: {book_id}")
        book, roadmap = self._load_book_and_roadmap(book_id)
        idx = 1
        results: List[dict] = []
        for mi, module in enumerate(roadmap.get("modules", [])):
            for ti, _topic in enumerate(module.get("topics", [])):
                log.info(f"Generating chapter {idx} of {len(roadmap.get('modules', [])) * len(module.get('topics',[]))} for book {book_id}...")
                out = self.generate_chapter(
                    user_id=user_id, wish_id=wish_id, book_id=book_id,
                    idx=idx, module_index=mi, topic_index=ti,
                )
                results.append({"idx": idx, **out})
                idx += 1
        log.info(f"Successfully completed 'generate_all_chapters' for book_id: {book_id}. Total chapters: {len(results)}")
        return {"count": len(results), "chapters": results}