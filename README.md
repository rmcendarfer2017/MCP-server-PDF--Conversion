# E-Book MCP Server

A Machine Comprehension Protocol (MCP) server for creating e-books and PDF documents from HTML content with embedded images.

## Features

- Convert HTML files to PDF documents with proper image handling
- Support for both pdfkit (wkhtmltopdf) and reportlab PDF generation engines
- Robust error handling and logging
- Temporary file management with automatic cleanup
- Detailed diagnostic information during conversion process

## Requirements

- Python 3.9+
- wkhtmltopdf (optional but recommended for better PDF quality)
- Python packages:
  - mcp
  - fastapi
  - python-docx
  - pdfkit
  - reportlab
  - pydantic
  - uvicorn

## Installation

1. Clone this repository
2. Install the required Python packages:
   ```
   pip install -r requirements.txt
   ```
3. Install wkhtmltopdf (optional):
   - Windows: Download and install from [wkhtmltopdf.org](https://wkhtmltopdf.org/downloads.html)
   - Linux: `sudo apt-get install wkhtmltopdf`
   - macOS: `brew install wkhtmltopdf`

## Usage

The server provides an MCP tool called `CREATE_DOC` that accepts the following arguments:

```json
{
  "text_file": "path/to/input.html",
  "images": {
    "image1.png": "path/to/image1.png",
    "image2.jpg": "path/to/image2.jpg"
  },
  "output_pdf": "path/to/output.pdf"
}
```

### Example

```python
result = await handle_call_tool("CREATE_DOC", {
    "text_file": "document.html",
    "images": {
        "header.png": "images/header.png",
        "footer.jpg": "images/footer.jpg"
    },
    "output_pdf": "output/document.pdf"
})
```

## How It Works

1. The server processes the input HTML file
2. It replaces image references in the HTML with absolute paths
3. It attempts to convert the HTML to PDF using pdfkit (if available)
4. If pdfkit fails or is not available, it falls back to reportlab
5. The resulting PDF is saved to the specified output path

## License

MIT