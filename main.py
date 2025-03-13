import os
import asyncio
import sys
import re
import uuid
from pathlib import Path
from typing import Dict

import pdfkit
from docx import Document
from docx.shared import Inches
from fastapi import FastAPI
from pydantic import BaseModel

from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server import NotificationOptions, Server
import mcp.server.stdio
import shutil

# Add reportlab for alternative PDF generation
try:
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image as ReportLabImage
    from reportlab.lib.styles import getSampleStyleSheet
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False
    print("Warning: reportlab not installed. Will try to use pdfkit only.", file=sys.stderr)

# Check if wkhtmltopdf is available
def is_wkhtmltopdf_available():
    try:
        import subprocess
        result = subprocess.run(['wkhtmltopdf', '-V'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        return False

WKHTMLTOPDF_AVAILABLE = is_wkhtmltopdf_available()
if not WKHTMLTOPDF_AVAILABLE:
    print("Warning: wkhtmltopdf not found in PATH. PDF conversion with pdfkit may fail.", file=sys.stderr)
    if not REPORTLAB_AVAILABLE:
        print("Warning: Neither wkhtmltopdf nor reportlab is available. PDF generation will likely fail.", file=sys.stderr)

# Initialize MCP Server with the name matching mcp_config.json
server = Server("e-book")

# Define FastAPI app (optional for monitoring)
app = FastAPI()

# Base directory for document storage
DOCS_DIR = Path("documents")
DOCS_DIR.mkdir(exist_ok=True)

# Store document metadata
documents: Dict[str, Dict] = {}

@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """
    List available tools for document creation.
    """
    return [
        types.Tool(
            name="CREATE_DOC",
            description="Create a document based on the provided text file and images",
            inputSchema={
                "type": "object",
                "properties": {
                    "text_file": {"type": "string"},
                    "images": {
                        "type": "object",
                        "additionalProperties": {"type": "string"}
                    },
                    "output_pdf": {"type": "string"},
                },
                "required": ["text_file", "output_pdf"],
            },
        ),
    ]

# Function to convert HTML to PDF using reportlab
def html_to_pdf_with_reportlab(html_path, pdf_path):
    """Convert HTML to PDF using reportlab when wkhtmltopdf is not available."""
    try:
        print(f"Converting HTML to PDF with reportlab: {html_path} -> {pdf_path}", file=sys.stderr)
        
        # Read HTML content
        with open(html_path, 'r', encoding='utf-8') as file:
            html_content = file.read()
        
        # Create a simple PDF document
        doc = SimpleDocTemplate(pdf_path, pagesize=letter)
        styles = getSampleStyleSheet()
        flowables = []
        
        # Add a title style
        title_style = styles['Heading1']
        title_style.alignment = 1  # Center alignment
        
        # Add a heading style
        heading_style = styles['Heading2']
        
        # Add a normal paragraph style
        normal_style = styles['Normal']
        
        # Extract CSS styles from the HTML
        css_styles = {}
        try:
            # Extract inline styles
            style_pattern = r'style="([^"]*)"'
            style_matches = re.findall(style_pattern, html_content)
            
            # Process style attributes
            for style_text in style_matches:
                # Parse style attributes
                style_attrs = {}
                for attr in style_text.split(';'):
                    if ':' in attr:
                        key, value = attr.split(':', 1)
                        style_attrs[key.strip()] = value.strip()
                
                # Store for later use
                css_styles[style_text] = style_attrs
                
            print(f"Extracted {len(css_styles)} CSS style definitions", file=sys.stderr)
        except Exception as css_error:
            print(f"Error extracting CSS: {str(css_error)}", file=sys.stderr)
        
        # Basic HTML parsing
        import re
        from html.parser import HTMLParser
        
        # Custom HTML Parser
        class StyleAwareHTMLParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.current_tag = None
                self.data_buffer = ""
                self.in_title = False
                self.in_heading = False
                self.in_paragraph = False
                self.in_list_item = False
                self.current_list_type = None  # 'ul' or 'ol'
                self.list_item_count = 0
                self.elements = []
                self.image_found = False
                self.current_style = {}  # Store current element's style
            
            def handle_starttag(self, tag, attrs):
                self.current_tag = tag
                attrs_dict = dict(attrs)
                
                # Extract style if present
                if 'style' in attrs_dict:
                    style_text = attrs_dict['style']
                    # Parse inline style
                    style_attrs = {}
                    for attr in style_text.split(';'):
                        if ':' in attr:
                            key, value = attr.split(':', 1)
                            style_attrs[key.strip()] = value.strip()
                    self.current_style = style_attrs
                else:
                    self.current_style = {}
                
                # Process different tags
                if tag == 'h1':
                    self.in_title = True
                    self.flush_buffer()
                elif tag in ['h2', 'h3']:
                    self.in_heading = True
                    self.flush_buffer()
                elif tag == 'p':
                    self.in_paragraph = True
                    self.flush_buffer()
                elif tag == 'img':
                    self.image_found = True
                    # Extract image attributes
                    if 'src' in attrs_dict:
                        self.elements.append(('img', attrs_dict['src'], attrs_dict.get('alt', ''), self.current_style))
                elif tag == 'ul' or tag == 'ol':
                    self.flush_buffer()
                    self.current_list_type = tag
                    self.list_item_count = 0
                    # Start a new list
                    self.elements.append(('list_start', tag, self.current_style))
                elif tag == 'li':
                    self.flush_buffer()
                    self.in_list_item = True
                    self.list_item_count += 1
                elif tag == 'div':
                    # Handle div containers with styles
                    if self.current_style:
                        self.elements.append(('div_start', self.current_style))
                elif tag == 'br':
                    # Handle line breaks
                    self.elements.append(('linebreak', None))
            
            def handle_endtag(self, tag):
                if tag == 'h1':
                    self.in_title = False
                    self.add_element('title', self.data_buffer)
                elif tag in ['h2', 'h3']:
                    self.in_heading = False
                    self.add_element('heading', self.data_buffer)
                elif tag == 'p':
                    self.in_paragraph = False
                    self.add_element('paragraph', self.data_buffer)
                elif tag == 'li':
                    self.in_list_item = False
                    # Add list item with its content and the list type
                    self.add_element('list_item', self.data_buffer, self.current_list_type, self.list_item_count)
                elif tag == 'ul' or tag == 'ol':
                    # End the list
                    self.current_list_type = None
                    self.list_item_count = 0
                    self.elements.append(('list_end', tag))
                elif tag == 'div':
                    # Close div container
                    self.elements.append(('div_end', None))
                
                self.data_buffer = ""
                self.current_tag = None
                self.current_style = {}
            
            def handle_data(self, data):
                if self.current_tag in ['h1', 'h2', 'h3', 'p', 'li']:
                    self.data_buffer += data
            
            def flush_buffer(self):
                if self.data_buffer.strip():
                    if self.in_title:
                        self.add_element('title', self.data_buffer)
                    elif self.in_heading:
                        self.add_element('heading', self.data_buffer)
                    elif self.in_paragraph:
                        self.add_element('paragraph', self.data_buffer)
                    elif self.in_list_item:
                        self.add_element('list_item', self.data_buffer, self.current_list_type, self.list_item_count)
                    self.data_buffer = ""
            
            def add_element(self, element_type, content, list_type=None, list_item_number=None):
                if content.strip():
                    if element_type == 'list_item':
                        self.elements.append((element_type, content.strip(), list_type, list_item_number, self.current_style))
                    else:
                        self.elements.append((element_type, content.strip(), self.current_style))
        
        # Parse HTML
        parser = StyleAwareHTMLParser()
        try:
            parser.feed(html_content)
        except Exception as parse_error:
            print(f"Warning: HTML parsing error: {str(parse_error)}. Will try simpler approach.", file=sys.stderr)
            # If parsing fails, fall back to a simpler approach
            flowables.append(Paragraph("HTML Parsing Error - Using simplified content", styles['Heading1']))
            # Simple text extraction
            text_content = re.sub(r'<[^>]*>', ' ', html_content)
            paragraphs = text_content.split('\n\n')
            for para in paragraphs:
                if para.strip():
                    flowables.append(Paragraph(para.strip(), normal_style))
                    flowables.append(Spacer(1, 10))
        
        # Helper function to apply styles to paragraphs
        def apply_style_to_paragraph(text, base_style, css_style):
            style = base_style
            
            # Create a copy of the base style to modify
            from copy import deepcopy
            from reportlab.lib.colors import HexColor, black
            from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
            
            custom_style = deepcopy(base_style)
            
            try:
                # Apply CSS styling to reportlab style
                if 'color' in css_style:
                    color_value = css_style['color']
                    if color_value.startswith('#'):
                        custom_style.textColor = HexColor(color_value)
                    elif color_value == 'white':
                        custom_style.textColor = HexColor('#FFFFFF')
                    elif color_value == 'black':
                        custom_style.textColor = black
                
                if 'font-size' in css_style:
                    size_text = css_style['font-size']
                    if 'rem' in size_text:
                        size = float(size_text.replace('rem', '')) * 12  # Base size is 12pt
                        custom_style.fontSize = size
                    elif 'px' in size_text:
                        size = float(size_text.replace('px', '')) * 0.75  # Convert px to pt (approx)
                        custom_style.fontSize = size
                
                if 'text-align' in css_style:
                    align = css_style['text-align']
                    if align == 'center':
                        custom_style.alignment = TA_CENTER
                    elif align == 'right':
                        custom_style.alignment = TA_RIGHT
                    elif align == 'left':
                        custom_style.alignment = TA_LEFT
                
                if 'font-weight' in css_style and css_style['font-weight'] == 'bold':
                    custom_style.fontName = 'Helvetica-Bold'
                
                if 'margin-bottom' in css_style or 'margin-top' in css_style:
                    # We'll handle spacing separately with Spacer elements
                    pass
                
                style = custom_style
            except Exception as style_error:
                print(f"Error applying style: {str(style_error)}", file=sys.stderr)
            
            return style
        
        # Process parsed elements
        current_list_style = None
        for element in parser.elements:
            element_type = element[0]
            
            if element_type == 'title':
                content = element[1]
                css_style = element[2] if len(element) > 2 else {}
                style = apply_style_to_paragraph(content, title_style, css_style)
                flowables.append(Paragraph(content, style))
                flowables.append(Spacer(1, 16))
            elif element_type == 'heading':
                content = element[1]
                css_style = element[2] if len(element) > 2 else {}
                style = apply_style_to_paragraph(content, heading_style, css_style)
                flowables.append(Paragraph(content, style))
                flowables.append(Spacer(1, 12))
            elif element_type == 'paragraph':
                content = element[1]
                css_style = element[2] if len(element) > 2 else {}
                style = apply_style_to_paragraph(content, normal_style, css_style)
                flowables.append(Paragraph(content, style))
                flowables.append(Spacer(1, 10))
            elif element_type == 'list_start':
                list_type = element[1]
                css_style = element[2] if len(element) > 2 else {}
                # Create appropriate list style
                if list_type == 'ul':
                    current_list_style = styles['Bullet']
                else:  # ol
                    current_list_style = styles['OrderedList']
            elif element_type == 'list_item':
                content = element[1]
                list_type = element[2]
                list_item_number = element[3]
                css_style = element[4] if len(element) > 4 else {}
                
                # Format list item based on type
                if list_type == 'ul':
                    # Bullet list
                    bullet_text = f"• {content}"
                    style = apply_style_to_paragraph(bullet_text, styles['Bullet'], css_style)
                    flowables.append(Paragraph(bullet_text, style))
                else:  # ol
                    # Numbered list
                    number_text = f"{list_item_number}. {content}"
                    style = apply_style_to_paragraph(number_text, styles['OrderedList'], css_style)
                    flowables.append(Paragraph(number_text, style))
                
                flowables.append(Spacer(1, 6))  # Smaller space between list items
            elif element_type == 'list_end':
                # Add some space after the list
                flowables.append(Spacer(1, 10))
                current_list_style = None
            elif element_type == 'img':
                img_path = element[1]
                img_alt = element[2]
                css_style = element[3] if len(element) > 3 else {}
                
                # Handle local file paths
                img_path_obj = Path(img_path)
                if img_path_obj.exists():
                    try:
                        print(f"Adding image to PDF: {img_path}", file=sys.stderr)
                        # Check if the file is actually an image
                        from PIL import Image as PILImage
                        # Try to open the image to verify it's valid
                        with PILImage.open(img_path_obj) as img_check:
                            img_width, img_height = img_check.size
                        
                        # If we get here, the image is valid
                        # Apply CSS styling for width/height if available
                        width = 400  # Default width
                        height = 300  # Default height
                        
                        if 'max-width' in css_style:
                            width_text = css_style['max-width']
                            if 'px' in width_text:
                                width = float(width_text.replace('px', ''))
                        
                        # Maintain aspect ratio
                        if img_width > 0 and img_height > 0:
                            aspect = img_height / img_width
                            height = width * aspect
                        
                        img = ReportLabImage(img_path, width=width, height=height)
                        flowables.append(img)
                        if img_alt:
                            flowables.append(Paragraph(img_alt, styles['Italic']))
                        flowables.append(Spacer(1, 12))
                    except Exception as img_check_error:
                        print(f"Warning: File exists but is not a valid image: {img_path}", file=sys.stderr)
                        print(f"Error details: {str(img_check_error)}", file=sys.stderr)
                        # Add a placeholder for the invalid image
                        flowables.append(Paragraph(f"[Image could not be loaded: {img_path}]", styles['Italic']))
                        flowables.append(Spacer(1, 12))
                else:
                    print(f"Warning: Image file not found: {img_path}", file=sys.stderr)
                    # Add a placeholder for the missing image
                    flowables.append(Paragraph(f"[Image not found: {img_path}]", styles['Italic']))
                    flowables.append(Spacer(1, 12))
            elif element_type == 'linebreak':
                flowables.append(Spacer(1, 10))
            elif element_type == 'div_start' or element_type == 'div_end':
                # Currently we don't handle div containers specially
                # but we could add special handling for background colors, etc.
                pass
        
        # If we have no flowables, add a default message
        if not flowables:
            flowables.append(Paragraph("No content could be extracted from the HTML", styles['Heading1']))
        
        # Build the PDF
        print(f"Building PDF with {len(flowables)} elements", file=sys.stderr)
        try:
            doc.build(flowables)
        except Exception as build_error:
            print(f"Error building PDF: {str(build_error)}", file=sys.stderr)
            # Try a simpler approach with just text
            print("Attempting simplified PDF build with text only...", file=sys.stderr)
            
            # Create a new PDF with just text
            doc = SimpleDocTemplate(pdf_path, pagesize=letter)
            flowables = []
            
            # Add a title
            flowables.append(Paragraph("Document (Simplified Version)", styles['Heading1']))
            flowables.append(Spacer(1, 20))
            
            # Extract text from HTML
            text_content = re.sub(r'<[^>]*>', ' ', html_content)
            paragraphs = text_content.split('\n\n')
            
            for para in paragraphs:
                if para.strip():
                    # Sanitize the text to remove problematic characters
                    clean_para = para.strip()
                    try:
                        flowables.append(Paragraph(clean_para, normal_style))
                        flowables.append(Spacer(1, 10))
                    except Exception as para_error:
                        print(f"Error adding paragraph: {str(para_error)}", file=sys.stderr)
            
            # Try to build the simplified PDF
            try:
                doc.build(flowables)
            except Exception as final_error:
                print(f"Final attempt to build PDF failed: {str(final_error)}", file=sys.stderr)
                return False
        
        # Verify PDF was created
        pdf_file = Path(pdf_path)
        if pdf_file.exists():
            print(f"PDF created successfully: {pdf_path} ({pdf_file.stat().st_size} bytes)", file=sys.stderr)
            return True
        else:
            print(f"PDF file not found after build: {pdf_path}", file=sys.stderr)
            return False
            
    except Exception as e:
        print(f"Error in reportlab PDF conversion: {str(e)}", file=sys.stderr)
        import traceback
        print(traceback.format_exc(), file=sys.stderr)
        return False

@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict | None
) -> list[types.TextContent | types.EmbeddedResource]:
    """Handle tool execution."""
    print(f"Handling tool call: {name} with arguments: {arguments}", file=sys.stderr)
    
    if name == "CREATE_DOC":
        resources = []
        
        if arguments is None:
            resources.append(types.TextContent(
                type="text",
                text="Error: No arguments provided"
            ))
            return resources
        
        # Get text file path
        text_file_path = arguments.get("text_file")
        if not text_file_path:
            resources.append(types.TextContent(
                type="text",
                text="Error: No text file provided"
            ))
            return resources
        
        print(f"Processing text file: {text_file_path}", file=sys.stderr)
        
        # Get output PDF path
        output_pdf_path = arguments.get("output_pdf")
        if not output_pdf_path:
            # Default to same directory as text file with .pdf extension
            output_pdf_path = os.path.splitext(text_file_path)[0] + ".pdf"
        
        print(f"Output PDF path: {output_pdf_path}", file=sys.stderr)
        
        # Get images dictionary
        images = arguments.get("images", {})
        print(f"Images: {images}", file=sys.stderr)
        
        # Check if text file exists
        if not os.path.exists(text_file_path):
            resources.append(types.TextContent(
                type="text",
                text=f"Error: Text file not found: {text_file_path}"
            ))
            return resources
        
        # Check if images exist
        for img_name, img_path in images.items():
            if not os.path.exists(img_path):
                resources.append(types.TextContent(
                    type="text",
                    text=f"Warning: Image not found: {img_path}"
                ))
        
        # Create a temporary directory for processing
        temp_dir = os.path.join(os.path.dirname(output_pdf_path), "temp_" + str(uuid.uuid4()))
        os.makedirs(temp_dir, exist_ok=True)
        print(f"Created temporary directory: {temp_dir}", file=sys.stderr)
        
        # Determine file type and process accordingly
        _, ext = os.path.splitext(text_file_path)
        
        # Process HTML file
        if ext.lower() in [".html", ".htm"]:
            # Read the HTML content
            with open(text_file_path, "r", encoding="utf-8") as f:
                html_content = f.read()
            
            # Process image references in HTML
            img_pattern = r'<img\s+[^>]*src=["\']([^"\']+)["\'][^>]*>'
            
            # Find all image references
            img_matches = re.finditer(img_pattern, html_content)
            
            # Replace image references with absolute paths
            replacements = 0
            for match in img_matches:
                img_src = match.group(1)
                if img_src in images:
                    abs_path = images[img_src]
                    print(f"Replaced image reference: {img_src} -> {abs_path}", file=sys.stderr)
                    html_content = html_content.replace(f'src="{img_src}"', f'src="{abs_path}"')
                    html_content = html_content.replace(f"src='{img_src}'", f"src='{abs_path}'")
                    replacements += 1
            
            # If no replacements were made, log a message but don't add images to the bottom
            if replacements == 0 and images:
                print("No image references found in the HTML. Images will not be added automatically.", file=sys.stderr)
                # We're not adding images at the bottom of the document
            
            # Write the processed HTML to the temporary directory
            doc_path = os.path.join(temp_dir, os.path.basename(text_file_path))
            with open(doc_path, "w", encoding="utf-8") as f:
                f.write(html_content)
            print(f"Created HTML file with processed image references: {doc_path}", file=sys.stderr)
            
            # Convert HTML to PDF
            pdf_created = False
            
            # Try pdfkit first if wkhtmltopdf is available
            if WKHTMLTOPDF_AVAILABLE:
                try:
                    print(f"Converting HTML to PDF with pdfkit: {doc_path} -> {output_pdf_path}", file=sys.stderr)
                    # Configure pdfkit options to properly handle CSS
                    options = {
                        'enable-local-file-access': True,  # Allow access to local files (for images)
                        'quiet': '',
                        'print-media-type': True,  # Use print media CSS
                        'enable-javascript': True,
                        'javascript-delay': 1000,  # Wait for JavaScript execution
                        'no-stop-slow-scripts': True,
                        'debug-javascript': True,
                        'load-error-handling': 'ignore',
                        'load-media-error-handling': 'ignore'
                    }
                    pdfkit.from_file(doc_path, output_pdf_path, options=options)
                    if os.path.exists(output_pdf_path) and os.path.getsize(output_pdf_path) > 0:
                        print(f"pdfkit conversion successful", file=sys.stderr)
                        pdf_created = True
                    else:
                        print(f"pdfkit conversion failed: output file is empty or not created", file=sys.stderr)
                except Exception as e:
                    print(f"Error converting HTML to PDF with pdfkit: {str(e)}", file=sys.stderr)
            else:
                print(f"pdfkit not available, skipping pdfkit conversion", file=sys.stderr)
            
            # If pdfkit failed or is not available, try reportlab
            if not pdf_created and REPORTLAB_AVAILABLE:
                print(f"Attempting conversion with reportlab", file=sys.stderr)
                try:
                    if html_to_pdf_with_reportlab(doc_path, output_pdf_path):
                        print(f"reportlab conversion successful", file=sys.stderr)
                        pdf_created = True
                    else:
                        print(f"reportlab conversion failed", file=sys.stderr)
                except Exception as e:
                    print(f"Error converting HTML to PDF with reportlab: {str(e)}", file=sys.stderr)
            
            # Check if PDF was created
            if pdf_created:
                print(f"Successfully created PDF: {output_pdf_path}", file=sys.stderr)
                
                # Return the PDF path as text content instead of trying to embed it as a resource
                resources.append(types.TextContent(
                    type="text",
                    text=f"PDF created successfully: {output_pdf_path}"
                ))
                
                # Add a link to the HTML source
                resources.append(types.TextContent(
                    type="text",
                    text=f"HTML source: {doc_path}"
                ))
                
                # Add success message
                resources.append(types.TextContent(
                    type="text",
                    text=f"Successfully converted {text_file_path} to PDF with {len(images)} images."
                ))
            else:
                # If PDF creation failed, return error message
                resources.append(types.TextContent(
                    type="text",
                    text=f"Error: Failed to create PDF from {text_file_path}. Please check the logs for details."
                ))
                
                # Add suggestions for troubleshooting
                resources.append(types.TextContent(
                    type="text",
                    text="Troubleshooting suggestions:\n"
                         "1. Make sure wkhtmltopdf is installed and in your PATH\n"
                         "2. Check that all images exist and are accessible\n"
                         "3. Verify that the HTML file is valid and properly formatted"
                ))
        else:
            resources.append(types.TextContent(
                type="text",
                text=f"Error: Unsupported file type: {ext}. Only HTML files are supported."
            ))
        
        # Clean up temporary directory
        try:
            shutil.rmtree(temp_dir)
            print(f"Cleaned up temporary directory: {temp_dir}", file=sys.stderr)
        except Exception as e:
            print(f"Error cleaning up temporary directory: {str(e)}", file=sys.stderr)
        
        return resources
    
    # If tool not recognized
    return [types.TextContent(
        type="text",
        text=f"Error: Unknown tool: {name}"
    )]

async def main():
    # Run the server using stdin/stdout streams
    try:
        # Create documents directory if it doesn't exist
        DOCS_DIR.mkdir(exist_ok=True)
        
        print("Starting e-book MCP server...", file=sys.stderr)
        print(f"wkhtmltopdf available: {WKHTMLTOPDF_AVAILABLE}", file=sys.stderr)
        print(f"reportlab available: {REPORTLAB_AVAILABLE}", file=sys.stderr)
        
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="e-book",
                    server_version="0.1.0",
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )
    except Exception as e:
        import traceback
        print(f"Error running MCP server: {str(e)}", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)
        sys.exit(1)

# Add an entry point to run the main function when the script is run directly
if __name__ == "__main__":
    asyncio.run(main())
