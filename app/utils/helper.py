import re

def remove_citations(md_text: str) -> str:
    """
    Post-processing function to remove all citations from the Markdown text using regex.
    Citations are assumed to be in the form: ([link](url), [link](url))
    """
    pattern = r'\(\s*(\[[^\]]+\]\([^\)]+\)\s*(?:,\s*\[[^\]]+\]\([^\)]+\)\s*)*)\)'
    cleaned_text = re.sub(pattern, '', md_text)
    return cleaned_text