import re

def is_scanned_pdf(text, num_pages):
    """
    Detect if a PDF is likely scanned (image-based) by examining the extracted text.
    Image-based PDFs usually have very little text extracted by normal PDF parsers.
    """
    if num_pages == 0:
        return False
        
    # Remove whitespace
    clean_text = re.sub(r'\s+', '', text)
    
    # If there are fewer than 100 characters per page on average, it's highly likely scanned
    if len(clean_text) < num_pages * 100:
        return True
        
    return False
