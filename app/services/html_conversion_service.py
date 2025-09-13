# app/services/html_conversion_service.py
"""
HTML Conversion Service

This service converts markdown files to styled HTML and stores them in the appropriate location.
It fetches markdown content from the chapters table, converts it to HTML using the styled converter,
and saves the HTML to the 'html' bucket with the proper path structure.
"""

from __future__ import annotations
import json
import uuid
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import markdown2
from fastapi import HTTPException, status
from supabase import Client

from app.core.config import settings
from app.core.logging import get_logger

# Instantiate the logger for this specific module
log = get_logger(__name__)


def convert_markdown_to_styled_html(markdown_text: str, as_fragment: bool = False) -> str:
    """
    Converts a Markdown string to styled HTML. If as_fragment is True, it returns
    only the core HTML content without the container div, which is ideal for PDF assembly.
    """
    styles = {
        'body': {
            'background-color': '#fdf6e3',
            'font-family': "'Merriweather', serif",
            'line-height': '1.7',
            'margin': '0',
            'padding': '0',
        },
        'container': {
            'max-width': '680px',
            'margin': '20px auto',
            'padding': '40px',
            'background-color': '#fdfdfa',
            'border': '1px solid #d9d5ce',
            'border-radius': '3px',
            'box-shadow': '0 5px 15px rgba(0,0,0,0.1)',
        },
        'h1': {
            'font-family': "'Playfair Display', serif",
            'color': '#3a3a3a',
            'font-size': '2.5em',
            'text-align': 'center',
            'margin-bottom': '20px',
            'line-height': '1.2',
            'border-bottom': '2px solid #c59b6d',
            'padding-bottom': '10px',
        },
        'h2': {
            'font-family': "'Playfair Display', serif",
            'color': '#8c4b4b',
            'font-size': '1.8em',
            'margin-top': '2em',
            'margin-bottom': '0.8em',
            'border-bottom': '1px double #d9d5ce',
            'padding-bottom': '0.3em',
        },
        'p': {
            'font-size': '1em',
            'margin-bottom': '1.2em',
            'text-align': 'justify',
            'hyphens': 'auto',
        },
        'ul': {'padding-left': '25px', 'margin-bottom': '1.2em'},
        'li': {'margin-bottom': '0.6em', 'text-align': 'left'},
        'a': {'color': '#c59b6d', 'text-decoration': 'underline'}
    }

    def style_dict_to_string(style_dict):
        return '; '.join([f'{key}: {value}' for key, value in style_dict.items()])

    processed_html = markdown2.markdown(markdown_text)

    # Apply inline styles to the core HTML tags
    for tag, style_dict in styles.items():
        if tag not in ['body', 'container']:
            style_string = style_dict_to_string(style_dict)
            processed_html = processed_html.replace(f'<{tag}>', f'<{tag} style="{style_string}">')

    if as_fragment:
        return processed_html
    
    final_html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <link href="https://fonts.googleapis.com/css2?family=Merriweather:wght@400;700&family=Playfair+Display:wght@700&display=swap" rel="stylesheet">
        <title>Article</title>
        <style>
            @media screen and (max-width: 600px) {{
                .container {{ padding: 20px !important; margin: 0 !important; width: 100% !important; border-radius: 0 !important; }}
                h1 {{ font-size: 2em !important; }}
                h2 {{ font-size: 1.5em !important; }}
                body {{ padding: 0 !important; }}
            }}
        </style>
    </head>
    <body style="{style_dict_to_string(styles['body'])}">
        <div class="container" style="{style_dict_to_string(styles['container'])}">
            {processed_html}
        </div>
    </body>
    </html>
    """
    return final_html

class HTMLConversionService:
    """
    Service for converting markdown content to styled HTML and managing storage.
    """
    
    def __init__(self, supabase: Client):
        self.sb = supabase
        self.html_bucket = "html"
        
    def _download_markdown_content(self, content_path: str) -> str:
        """
        Downloads markdown content from storage using the content_path.
        
        Args:
            content_path: The storage path in format "bucket/path/to/file.md"
            
        Returns:
            The markdown content as a string
            
        Raises:
            HTTPException: If the file cannot be downloaded or parsed
        """
        try:
            # Parse the content_path to extract bucket and path
            if "/" not in content_path:
                raise HTTPException(
                    status_code=422, 
                    detail="Invalid content_path format. Expected 'bucket/path'"
                )
            
            bucket, *path_parts = content_path.split("/")
            path = "/".join(path_parts)
            
            log.info(f"Downloading markdown from storage at {bucket}/{path}")
            raw_content = self.sb.storage.from_(bucket).download(path)
            markdown_content = raw_content.decode("utf-8")
            
            log.info(f"Successfully downloaded markdown content ({len(markdown_content)} characters)")
            return markdown_content
            
        except Exception as e:
            log.error(f"Failed to download markdown content from {content_path}: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to download markdown content: {str(e)}"
            )
    
    def _upload_html_content(self, book_id: str, chapter_id: str, idx: int, html_content: str) -> str:
        """
        Uploads HTML content to the html bucket with the proper path structure.
        
        Args:
            book_id: The book ID
            chapter_id: The chapter ID  
            idx: The chapter index
            html_content: The HTML content to upload
            
        Returns:
            The storage path where the HTML was saved
            
        Raises:
            HTTPException: If the upload fails
        """
        try:
            # Create the path structure: books/[book_id]/chapters/[chapter_id]/[idx].html
            path_in_bucket = f"books/{book_id}/chapters/{chapter_id}/{idx}.html"
            
            log.info(f"Uploading HTML to storage at {self.html_bucket}/{path_in_bucket}")
            data = html_content.encode("utf-8")
            
            try:
                self.sb.storage.from_(self.html_bucket).upload(
                    path=path_in_bucket, 
                    file=data, 
                    file_options={"content-type": "text/html"}
                )
            except Exception:
                # Overwrite if it exists
                log.warning(f"File already exists at {self.html_bucket}/{path_in_bucket}. Overwriting.")
                self.sb.storage.from_(self.html_bucket).update(
                    path=path_in_bucket, 
                    file=data, 
                    file_options={"content-type": "text/html"}
                )
            
            storage_url = f"{self.html_bucket}/{path_in_bucket}"
            log.info(f"Successfully uploaded HTML content to {storage_url}")
            return storage_url
            
        except Exception as e:
            log.error(f"Failed to upload HTML content: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to upload HTML content: {str(e)}"
            )
    
    def _update_chapter_html_path(self, chapter_id: str, html_path: str) -> dict:
        """
        Updates the html_path column in the chapters table.
        
        Args:
            chapter_id: The chapter ID to update
            html_path: The new HTML path to store
            
        Returns:
            The updated chapter record
            
        Raises:
            HTTPException: If the update fails
        """
        try:
            log.info(f"Updating html_path for chapter_id: {chapter_id}")
            
            updated = self.sb.table("chapters").update({
                "html_path": html_path,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", chapter_id).execute()
            
            if not updated.data:
                raise HTTPException(
                    status_code=404,
                    detail=f"Chapter with id {chapter_id} not found"
                )
            
            log.info(f"Successfully updated html_path for chapter_id: {chapter_id}")
            return updated.data[0]
            
        except HTTPException:
            raise
        except Exception as e:
            log.error(f"Failed to update html_path for chapter_id {chapter_id}: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to update chapter html_path: {str(e)}"
            )
    
    def _get_chapters_by_book_id(self, book_id: str) -> List[dict]:
        """
        Fetches all chapters for a given book_id from the database.
        
        Args:
            book_id: The book ID to fetch chapters for
            
        Returns:
            List of chapter records
            
        Raises:
            HTTPException: If the query fails
        """
        try:
            log.info(f"Fetching chapters for book_id: {book_id}")
            
            response = self.sb.table("chapters").select("*").eq("book_id", book_id).execute()
            chapters = response.data
            
            log.info(f"Found {len(chapters)} chapters for book_id: {book_id}")
            return chapters
            
        except Exception as e:
            log.error(f"Failed to fetch chapters for book_id {book_id}: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to fetch chapters: {str(e)}"
            )
    
    def convert_chapter_to_html(self, chapter_id: str) -> dict:
        """
        Converts a single chapter from markdown to HTML and updates the database.
        
        Args:
            chapter_id: The chapter ID to convert
            
        Returns:
            Dictionary containing the conversion result
            
        Raises:
            HTTPException: If the conversion fails
        """
        try:
            log.info(f"Starting HTML conversion for chapter_id: {chapter_id}")
            
            # Get the chapter record
            chapter_response = self.sb.table("chapters").select("*").eq("id", chapter_id).single().execute()
            if not chapter_response.data:
                raise HTTPException(
                    status_code=404,
                    detail=f"Chapter with id {chapter_id} not found"
                )
            
            chapter = chapter_response.data
            content_path = chapter.get("content_path")
            
            if not content_path:
                raise HTTPException(
                    status_code=422,
                    detail=f"Chapter {chapter_id} has no content_path"
                )
            
            # Download markdown content
            markdown_content = self._download_markdown_content(content_path)
            
            # Convert to HTML
            html_content = convert_markdown_to_styled_html(markdown_content)
            
            # Upload HTML content
            book_id = chapter["book_id"]
            idx = chapter["idx"]
            html_path = self._upload_html_content(book_id, chapter_id, idx, html_content)
            
            # Update the database
            updated_chapter = self._update_chapter_html_path(chapter_id, html_path)
            
            log.info(f"Successfully converted chapter_id: {chapter_id} to HTML")
            return {
                "chapter_id": chapter_id,
                "html_path": html_path,
                "status": "success",
                "updated_chapter": updated_chapter
            }
            
        except HTTPException:
            raise
        except Exception as e:
            log.error(f"Failed to convert chapter_id {chapter_id} to HTML: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to convert chapter to HTML: {str(e)}"
            )
    
    def convert_book_chapters_to_html(self, book_id: str) -> dict:
        """
        Converts all chapters for a given book from markdown to HTML.
        
        Args:
            book_id: The book ID to convert chapters for
            
        Returns:
            Dictionary containing the conversion results
            
        Raises:
            HTTPException: If the conversion fails
        """
        try:
            log.info(f"Starting HTML conversion for all chapters in book_id: {book_id}")
            
            # Get all chapters for the book
            chapters = self._get_chapters_by_book_id(book_id)
            
            if not chapters:
                log.warning(f"No chapters found for book_id: {book_id}")
                return {
                    "book_id": book_id,
                    "total_chapters": 0,
                    "converted_chapters": 0,
                    "failed_chapters": 0,
                    "results": []
                }
            
            results = []
            converted_count = 0
            failed_count = 0
            
            for chapter in chapters:
                chapter_id = chapter["id"]
                try:
                    result = self.convert_chapter_to_html(chapter_id)
                    results.append(result)
                    converted_count += 1
                except Exception as e:
                    log.error(f"Failed to convert chapter_id {chapter_id}: {e}")
                    results.append({
                        "chapter_id": chapter_id,
                        "status": "failed",
                        "error": str(e)
                    })
                    failed_count += 1
            
            log.info(f"Completed HTML conversion for book_id: {book_id}. "
                    f"Converted: {converted_count}, Failed: {failed_count}")
            
            return {
                "book_id": book_id,
                "total_chapters": len(chapters),
                "converted_chapters": converted_count,
                "failed_chapters": failed_count,
                "results": results
            }
            
        except HTTPException:
            raise
        except Exception as e:
            log.error(f"Failed to convert chapters for book_id {book_id}: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to convert book chapters to HTML: {str(e)}"
            )

if __name__ == "__main__":
    # Example usage
    from supabase import create_client

    supabase_url = settings.SUPABASE_URL
    supabase_key = settings.SUPABASE_SERVICE_ROLE_KEY
    supabase = create_client(supabase_url, supabase_key)

    html_service = HTMLConversionService(supabase)
    
    # Convert a single chapter
    chapter_id = "41d6b6f4-3416-4d0a-9bdd-26ea6a85aa3f"
    result = html_service.convert_chapter_to_html(chapter_id)
    print(result)
    
    # # Convert all chapters in a book
    # book_id = "your-book-id"
    # book_result = html_service.convert_book_chapters_to_html(book_id)
    # print(book_result)