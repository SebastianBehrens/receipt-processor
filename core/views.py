from pathlib import Path
from datetime import datetime
import zipfile
import os
import base64
import requests
import re
import ast
import json
import logging
import traceback
import urllib.parse
from decimal import Decimal
from django.shortcuts import render, get_object_or_404, redirect
from django.http import HttpResponse, FileResponse, JsonResponse
from django.conf import settings
from django.views.decorators.http import require_GET, require_POST
from django.contrib.auth.decorators import login_required
from django.utils.text import get_valid_filename
from django.utils import timezone
from dotenv import load_dotenv
from .models import ReceiptSession, ExtractedFile, ReceiptItem, SortedItem, SessionAggregation
import unicodedata
from urllib.parse import quote

# Load environment variables from config/.env
load_dotenv(Path(settings.BASE_DIR) / 'config' / '.env')

# Set up logging
logger = logging.getLogger(__name__)

def get_or_create_session(user):
    """Get the user's current active session or create a new one."""
    # Try to get an incomplete session first
    session = ReceiptSession.objects.filter(
        user=user, 
        is_complete=False
    ).order_by('-created_at').first()
    
    if not session:
        # Create a new session
        session = ReceiptSession.objects.create(user=user)
        logger.info(f"Created new session {session.id} for user {user.username}")
    else:
        logger.debug(f"Using existing session {session.id} for user {user.username}")
    
    return session

def unzip_receipts(session, zip_filename):
    """Extract the uploaded ZIP file and create ExtractedFile objects."""
    if not zip_filename:
        return []
    
    # Get the base directory for uploads (updated to 0_uploaded)
    upload_dir = Path(settings.BASE_DIR) / 'data' / '0_uploaded'
    zip_path = upload_dir / zip_filename
    
    # Create extraction directory in 1_unzipped (same name as zip without extension)
    extract_dir_name = Path(zip_filename).stem
    extract_dir = Path(settings.BASE_DIR) / 'data' / '1_unzipped' / extract_dir_name
    
    # Create extraction directory if it doesn't exist
    extract_dir.mkdir(parents=True, exist_ok=True)
    
    extracted_files = []
    
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            # Extract all files
            zip_ref.extractall(extract_dir)
            
            # Get list of extracted files, filtering out macOS-specific files
            for file_path in extract_dir.rglob('*'):
                if file_path.is_file():
                    # Skip macOS-specific files and folders
                    if any(macos_file in str(file_path) for macos_file in ['__MACOSX', '.DS_Store', 'Thumbs.db']):
                        continue
                    
                    # Only include image files
                    file_extension = file_path.suffix.lower()
                    if file_extension not in ['.jpg', '.jpeg', '.png']:
                        continue
                    
                    # Get relative path from extraction directory
                    relative_path = file_path.relative_to(extract_dir)
                    
                    # Create ExtractedFile object
                    extracted_file, created = ExtractedFile.objects.get_or_create(
                        session=session,
                        filename=file_path.name,
                        defaults={
                            'relative_path': str(relative_path)
                        }
                    )
                    
                    if created:
                        logger.debug(f"Created ExtractedFile: {extracted_file.filename}")
                    
                    extracted_files.append(extracted_file)
    
    except Exception as e:
        logger.error(f"Error extracting ZIP file: {e}")
        return []
    
    logger.info(f"Extracted {len(extracted_files)} files for session {session.id}")
    return extracted_files

def get_current_file_info(session, request):
    """Get current file information."""
    current_file = request.session.get('current_file')
    if current_file:
        # Look up the ExtractedFile object 
        extracted_file = session.extracted_files.filter(filename=current_file).first()
        if extracted_file:
            return {
                'current_file': current_file
            }
    return {
        'current_file': None
    }

@login_required
@require_GET
def start_page(request):
    """Render the start page for the HTMX Receipt Processor."""
    session = get_or_create_session(request.user)
    
    # Get extracted files that haven't been processed yet
    unprocessed_files = session.extracted_files.filter(is_processed=False, is_skipped=False)
    
    # Get current file information
    current_file_info = get_current_file_info(session, request)
    
    # Create context with session information
    context = {
        'current_step': session.current_step,
        'extracted_files': list(unprocessed_files.values_list('filename', flat=True)),
        'session': session,
        'current_file': current_file_info['current_file'],
        'authelia_app_url': settings.AUTHELIA_APP_URL,
        'state': {
            'current_step': session.current_step,
            'receipt_zip': session.receipt_zip_filename,
            'payer': session.payer,
            'api_costs_total': float(session.api_costs_total),
            'current_extraction_index': session.current_extraction_index,
            'files_processed': session.files_processed,
            'progress_percentage': session.progress_percentage,
            'current_sort_index': session.current_sort_index,
            'extracted_files': list(unprocessed_files.values_list('filename', flat=True)),
            'current_file': current_file_info['current_file'],
            'current_file_normalized': current_file_info['current_file'],
            # Get consumption data for sorting/aggregation steps
            'consumption': get_consumption_data(session) if session.current_step >= 3 else {},
            'sort_items': get_sort_items(session) if session.current_step == 3 else [],
            'aggregation': get_aggregation_data(session) if session.current_step == 4 else {},
        }
    }
    
    return render(request, 'start_page.html', context)

def get_consumption_data(session):
    """Get consumption data organized by assignee for templates."""
    consumption = {'sebastian': [], 'iva': [], 'both': []}
    
    for sorted_item in session.sorted_items.select_related('receipt_item', 'receipt_item__source_file'):
        item_data = {
            'item': sorted_item.receipt_item.item_name,
            'price': str(sorted_item.receipt_item.price),
            'source_file': sorted_item.receipt_item.source_file.filename
        }
        consumption[sorted_item.assignee].append(item_data)
    
    return consumption

def get_sort_items(session):
    """Get items that need to be sorted."""
    # Get all confirmed receipt items that haven't been sorted yet
    unsorted_items = session.receipt_items.filter(
        is_confirmed=True,
        sorted_assignment__isnull=True
    ).select_related('source_file')
    
    sort_items = []
    for item in unsorted_items:
        sort_items.append({
            'item': item.item_name,
            'price': float(item.price),
            'source_file': item.source_file.filename,
            'id': item.id
        })
    
    return sort_items

def get_aggregation_data(session):
    """Get aggregation data for the session."""
    try:
        aggregation = session.aggregation
        return {
            'sebastian_total': float(aggregation.sebastian_total),
            'iva_total': float(aggregation.iva_total),
            'both_total': float(aggregation.both_total),
            'grand_total': float(aggregation.grand_total),
            'transfer_amount': float(aggregation.transfer_amount),
            'transfer_direction': aggregation.transfer_direction,
            'payer': session.payer
        }
    except SessionAggregation.DoesNotExist:
        return {}

@login_required
@require_POST
def step_view(request, step_number):
    """Main view to handle different steps of the receipt processing workflow."""
    logger.info(f"Step view requested: {step_number}")
    
    session = get_or_create_session(request.user)
    
    # Update current step (convert to 0-based index for internal use)
    session.current_step = step_number - 1
    session.save()
    
    # If moving to aggregation step (step 5 = index 4), calculate aggregation
    if step_number == 5:
        calculate_aggregation(session)
    
    # Calculate progress information for extraction step
    if step_number == 3 and session.extracted_files.exists():  # Step 3 is extraction (0-based index 2)
        unprocessed_files = session.extracted_files.filter(is_processed=False, is_skipped=False)
        total_files = session.extracted_files.count()
        files_processed = session.current_extraction_index
        progress_percentage = int((files_processed / total_files) * 100) if total_files > 0 else 0
        
        session.files_processed = files_processed
        session.progress_percentage = progress_percentage
        session.save()
        
        logger.debug(f"Extraction progress: {files_processed}/{total_files} ({progress_percentage}%)")
    
    # Get extracted files that haven't been processed yet
    unprocessed_files = session.extracted_files.filter(is_processed=False, is_skipped=False)
    
    # Get current file information
    current_file_info = get_current_file_info(session, request)
    
    # Create context with state information
    context = {
        'current_step': session.current_step,
        'extracted_files': list(unprocessed_files.values_list('filename', flat=True)),
        'session': session,
        'current_file': current_file_info['current_file'],
        'current_file_normalized': current_file_info['current_file'],
        'state': {
            'current_step': session.current_step,
            'receipt_zip': session.receipt_zip_filename,
            'payer': session.payer,
            'api_costs_total': float(session.api_costs_total),
            'current_extraction_index': session.current_extraction_index,
            'files_processed': session.files_processed,
            'progress_percentage': session.progress_percentage,
            'current_sort_index': session.current_sort_index,
            'extracted_files': list(unprocessed_files.values_list('filename', flat=True)),
            'current_file': current_file_info['current_file'],
            'current_file_normalized': current_file_info['current_file'],
            # Get consumption data for sorting/aggregation steps
            'consumption': get_consumption_data(session) if session.current_step >= 3 else {},
            'sort_items': get_sort_items(session) if session.current_step == 3 else [],
            'aggregation': get_aggregation_data(session) if session.current_step == 4 else {},
        }
    }
    
    logger.debug(f"Rendering step {step_number} with context keys: {list(context.keys())}")
    return render(request, 'start_page.html', context)

@login_required
@require_POST
def upload_files(request):
    """Handle file upload and return toast notification."""
    if request.FILES.get('receipt_file'):
        uploaded_file = request.FILES['receipt_file']
        
        # Get the payer from the form
        payer = request.POST.get('payer')
        
        # Check if file is a ZIP
        if not uploaded_file.name.lower().endswith('.zip'):
            return HttpResponse(f"""
                <div class="toast toast-end" id="error-toast">
                    <div class="alert alert-error">
                        <span>Please select a ZIP file. Try a different file.</span>
                    </div>
                </div>
                <script>
                    setTimeout(() => {{
                        const toast = document.getElementById('error-toast');
                        if (toast) toast.remove();
                    }}, 5000);
                </script>
            """, status=400)
        
        # Validate ZIP file by trying to read it
        try:
            zipfile.ZipFile(uploaded_file)
        except zipfile.BadZipFile:
            return HttpResponse(f"""
                <div class="toast toast-end" id="error-toast">
                    <div class="alert alert-error">
                        <span>Invalid ZIP file. Please try a different file.</span>
                    </div>
                </div>
                <script>
                    setTimeout(() => {{
                        const toast = document.getElementById('error-toast');
                        if (toast) toast.remove();
                    }}, 5000);
                </script>
            """, status=400)
        
        # Create upload directory if it doesn't exist
        upload_dir = Path(settings.BASE_DIR) / 'data' / '0_uploaded'
        upload_dir.mkdir(parents=True, exist_ok=True)
        
        # Sanitize the filename to prevent security issues
        filename = get_valid_filename(uploaded_file.name)
        file_path = upload_dir / filename
        
        # Save the file using Django's proper upload handling
        with open(file_path, 'wb+') as destination:
            for chunk in uploaded_file.chunks():
                destination.write(chunk)
        
        # Get or create session and store filename and payer
        session = get_or_create_session(request.user)
        session.receipt_zip_filename = filename
        session.payer = payer
        session.current_step = 2  # Move to Extract step
        session.save()
        
        # Extract the ZIP file and get list of files
        extracted_files = unzip_receipts(session, filename)
        
        # Get unprocessed files for context
        unprocessed_files = session.extracted_files.filter(is_processed=False, is_skipped=False)
        
        # Return the full page with updated state and success toast
        return render(request, 'start_page.html', {
            'current_step': session.current_step,
            'extracted_files': list(unprocessed_files.values_list('filename', flat=True)),
            'session': session,
            'state': {
                'current_step': session.current_step,
                'receipt_zip': session.receipt_zip_filename,
                'payer': session.payer,
                'api_costs_total': float(session.api_costs_total),
                'current_extraction_index': session.current_extraction_index,
                'files_processed': session.files_processed,
                'progress_percentage': session.progress_percentage,
                'current_sort_index': session.current_sort_index,
                'extracted_files': list(unprocessed_files.values_list('filename', flat=True)),
                'consumption': get_consumption_data(session) if session.current_step >= 3 else {},
                'sort_items': get_sort_items(session) if session.current_step == 3 else [],
                'aggregation': get_aggregation_data(session) if session.current_step == 4 else {},
            }
        })
    
    # If no file uploaded, return error toast
    return HttpResponse("""
        <div class="toast toast-end" id="error-toast">
            <div class="alert alert-error">
                <span>No file selected. Please try again.</span>
            </div>
        </div>
        <script>
            setTimeout(() => {
                const toast = document.getElementById('error-toast');
                if (toast) toast.remove();
            }, 5000);
        </script>
    """, status=400)

@login_required
@require_POST
def restart(request):
    """Reset the state and return to step 1."""
    # Mark the current session as complete (if exists)
    current_session = ReceiptSession.objects.filter(
        user=request.user, 
        is_complete=False
    ).order_by('-created_at').first()
    
    if current_session:
        current_session.is_complete = True
        current_session.save()
        logger.info(f"Marked session {current_session.id} as complete")
    
    # Create a new session
    session = ReceiptSession.objects.create(user=request.user)
    logger.info(f"Created new session {session.id} for restart")
    
    # Render the full page with reset state
    return render(request, 'start_page.html', {
        'current_step': session.current_step,
        'extracted_files': [],
        'session': session,
        'state': {
            'current_step': session.current_step,
            'receipt_zip': session.receipt_zip_filename,
            'payer': session.payer,
            'api_costs_total': float(session.api_costs_total),
            'current_extraction_index': session.current_extraction_index,
            'files_processed': session.files_processed,
            'progress_percentage': session.progress_percentage,
            'current_sort_index': session.current_sort_index,
            'extracted_files': [],
            'consumption': {},
            'sort_items': [],
            'aggregation': {},
        }
    })

@login_required
@require_GET
def get_step_template(request, step_number):
    """Return just the template content for HTMX updates."""
    session = get_or_create_session(request.user)
    
    if step_number == 1:
        return render(request, '1_read_the_docs.html')
    elif step_number == 2:
        return render(request, '2_upload_receipts.html')
    elif step_number == 3:
        return render(request, '3_extract_receipts.html')
    elif step_number == 4:
        return render(request, '4_sort.html', {
            'state': {
                'current_step': session.current_step,
                'receipt_zip': session.receipt_zip_filename,
                'payer': session.payer,
                'api_costs_total': float(session.api_costs_total),
                'current_extraction_index': session.current_extraction_index,
                'files_processed': session.files_processed,
                'progress_percentage': session.progress_percentage,
                'current_sort_index': session.current_sort_index,
                'consumption': get_consumption_data(session),
                'sort_items': get_sort_items(session),
                'aggregation': get_aggregation_data(session),
            }
        })
    elif step_number == 5:
        return render(request, '5_aggregate.html', {
            'state': {
                'current_step': session.current_step,
                'receipt_zip': session.receipt_zip_filename,
                'payer': session.payer,
                'api_costs_total': float(session.api_costs_total),
                'current_extraction_index': session.current_extraction_index,
                'files_processed': session.files_processed,
                'progress_percentage': session.progress_percentage,
                'current_sort_index': session.current_sort_index,
                'consumption': get_consumption_data(session),
                'sort_items': get_sort_items(session),
                'aggregation': get_aggregation_data(session),
            }
        })
    else:
        # Default to step 1 if invalid step number
        return render(request, '1_read_the_docs.html')

@login_required
@require_POST
def select_file(request):
    """Handle file selection and return the updated main content area."""
    session = get_or_create_session(request.user)
    selected_file = request.POST.get('file')
    
    # Validate that the selected file belongs to this user's session
    if selected_file:
        extracted_file = session.extracted_files.filter(filename=selected_file).first()
        if extracted_file:
            # Store selected file in Django session (not our database session)
            request.session['selected_file'] = selected_file
            logger.info(f"Selected file: {selected_file}")
    
    # Return just the main content area with the selected file
    return render(request, '3_extract_receipts.html', {
        'selected_file': request.session.get('selected_file')
    })

@login_required
@require_GET
def serve_image(request, filename):
    """Serve the selected image file."""
    # The filename parameter is URL-encoded, so decode it
    decoded_filename = urllib.parse.unquote(filename)
    logger.debug(f"serve_image called with filename: '{filename}' (decoded: '{decoded_filename}')")
    
    session = get_or_create_session(request.user)
    
    if not session.receipt_zip_filename:
        logger.error("No file uploaded")
        return HttpResponse("No file uploaded", status=404)
    
    # Find the file using the decoded filename
    extracted_file = session.extracted_files.filter(filename=decoded_filename).first()
    if not extracted_file:
        logger.error(f"File not found in database. Looking for filename: {decoded_filename}")
        logger.error(f"Available files: {list(session.extracted_files.values_list('filename', flat=True))}")
        return HttpResponse("File not found or access denied", status=404)
    
    # Get the extraction directory and use the relative_path for the actual file path
    zip_filename = session.receipt_zip_filename
    extract_dir_name = Path(zip_filename).stem
    image_path = Path(settings.BASE_DIR) / 'data' / '1_unzipped' / extract_dir_name / extracted_file.relative_path
    
    logger.debug(f"Looking for image at: {image_path}")
    logger.debug(f"Image path exists: {image_path.exists()}")
    
    if not image_path.exists() or not image_path.is_file():
        logger.error(f"File not found on disk at: {image_path}")
        return HttpResponse("File not found", status=404)
    
    # Serve the file
    return FileResponse(open(image_path, 'rb'), content_type='image/jpeg')

def image_to_dataframe_dict(image_path, openai_api_key) -> tuple[list[dict], float]:
    """Extract receipt data from image using OpenAI API."""
    logger.debug(f"Starting image extraction for: {image_path}")
    logger.debug(f"Image path exists: {image_path.exists()}")
    
    try:
        # Encode the image
        logger.debug("Reading and encoding image...")
        with open(image_path, "rb") as image_file:
            encoded_image = base64.b64encode(image_file.read()).decode('utf-8')
        logger.debug(f"Image encoded successfully. Length: {len(encoded_image)}")

        # Prepare the API request
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {openai_api_key}"
        }

        payload = {
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Analyze this image of a receipt. Extract the items and their prices. "
                                "Ensure to account for discounts, which are often indicated by a minus sign "
                                "in front of the price or as a separate line item. Subtract any discounts from "
                                "the corresponding item's price. Return the result as a Python list of dictionaries, "
                                "where each dictionary has 'item' and 'price' keys. The 'price' should be a float. "
                                "Do not include any explanatory text, just the Python code for the list of dictionaries, "
                                "without the markdown formatting of the code."
                            )
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{encoded_image}"
                            }
                        }
                    ]
                }
            ],
            "max_tokens": 5000
        }

        logger.info("Making API request to OpenAI for image extraction")
        # Make the API request
        response = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)
        logger.debug(f"API response status: {response.status_code}")
        
        if response.status_code != 200:
            logger.error(f"API error response: {response.text}")
            raise Exception(f"OpenAI API error: {response.status_code} - {response.text}")

        response_json = response.json()
        logger.debug(f"Response JSON keys: {response_json.keys()}")
        
        tokens_used = response_json['usage']['total_tokens']
        request_cost = ((0.03/1000) * tokens_used)/4 # /4 empirically determined from https://platform.openai.com/usage

        # Extract and process the result
        result = response_json['choices'][0]['message']['content']
        logger.debug(f"Raw API result: {result}")

        # Extract the Python code from the markdown
        try:
            result_as_string = re.sub(r'\n', '', result)
            result = ast.literal_eval(result_as_string)

            out = ast.literal_eval(result_as_string)
            for item in out:
                item['price'] = str(item['price'])
            logger.info(f"Successfully extracted {len(out)} items from image")
        except Exception as parse_error:
            logger.error(f"Error parsing API result: {parse_error}")
            out = [{'item': 'Error in extraction. Proceed manually.', 'price': '0'}]
        finally:
            logger.debug(f"Returning {len(out)} items with cost ${request_cost:.4f}")
            return out, request_cost
    except Exception as e:
        logger.error(f"ERROR in image_to_dataframe_dict: {str(e)}")
        logger.error(f"TRACEBACK: {traceback.format_exc()}")
        raise

@login_required
@require_POST
def extract_image_data(request):
    """Extract receipt data from the selected image using OpenAI API."""
    logger.debug("Starting image data extraction")
    
    session = get_or_create_session(request.user)
    selected_file = request.session.get('selected_file')
    
    logger.info(f"Extracting data from file: {selected_file}")
    
    if not selected_file:
        logger.error("No file selected for extraction")
        return HttpResponse('<div class="alert alert-error">No file selected</div>', status=400)
    
    # Validate that the selected file belongs to this user's session
    extracted_file = session.extracted_files.filter(filename=selected_file).first()
    if not extracted_file:
        logger.error(f"File {selected_file} not found in user's session")
        return HttpResponse('<div class="alert alert-error">File not found or access denied</div>', status=404)
    
    # Check if we have the receipt_zip in session
    if not session.receipt_zip_filename:
        logger.error("No receipt ZIP file found in session")
        return HttpResponse('<div class="alert alert-error">No receipt ZIP file found in session</div>', status=400)
    
    # Get the image path
    zip_filename = session.receipt_zip_filename
    extract_dir_name = Path(zip_filename).stem
    image_path = Path(settings.BASE_DIR) / 'data' / '1_unzipped' / extract_dir_name / extracted_file.relative_path
    
    logger.debug(f"Zip filename: {zip_filename}")
    logger.debug(f"Extract dir name: {extract_dir_name}")
    logger.debug(f"Image path: {image_path}")
    logger.debug(f"Image path exists: {image_path.exists()}")
    
    if not image_path.exists():
        logger.error(f"Image file not found at: {image_path}")
        return HttpResponse(f'<div class="alert alert-error">Image file not found at: {image_path}</div>', status=404)
    
    # Get OpenAI API key from environment
    try:
        # Ensure .env is loaded
        load_dotenv(Path(settings.BASE_DIR) / 'config' / '.env')
        openai_api_key = os.environ['OPENAI_API_KEY']
        logger.debug(f"OpenAI API key loaded: {bool(openai_api_key)}")
    except KeyError:
        logger.error("OpenAI API key not configured")
        return HttpResponse('<div class="alert alert-error">OpenAI API key not configured</div>', status=500)
    
    try:
        logger.info(f"Starting AI extraction for {selected_file}")
        # Extract data from image
        extracted_data, cost = image_to_dataframe_dict(image_path, openai_api_key)
        logger.info(f"Extraction completed. Found {len(extracted_data)} items, cost: ${cost:.4f}")
        
        # Add cost to total API costs
        session.api_costs_total += Decimal(str(cost))
        session.save()
        logger.info(f"Total API costs now: ${session.api_costs_total:.4f}")
        
        # Store extracted data in Django session for later use
        request.session['extracted_data'] = extracted_data
        
        # Return the table template with next file button
        return render(request, 'extracted_data_table.html', {
            'extracted_data': extracted_data,
            'cost': cost,
            'selected_file': selected_file,
            'current_file': selected_file,
            'show_next_button': True
        })
    except Exception as e:
        error_details = traceback.format_exc()
        logger.error(f"Extraction failed for {selected_file}: {str(e)}")
        logger.error(f"TRACEBACK: {error_details}")
        return HttpResponse(f'<div class="alert alert-error">Extraction failed: {str(e)}</div>', status=500)

@login_required
@require_POST
def save_extraction(request):
    """Save the filtered extraction data to state."""
    try:
        session = get_or_create_session(request.user)
        file = request.POST.get('file')
        data_json = request.POST.get('data')
        
        if not file or not data_json:
            return JsonResponse({'error': 'Missing file or data'}, status=400)
        
        # Validate that the file belongs to this user's session
        extracted_file = session.extracted_files.filter(filename=file).first()
        if not extracted_file:
            return JsonResponse({'error': 'File not found or access denied'}, status=404)
        
        # Parse the JSON data
        filtered_data = json.loads(data_json)
        
        # Delete existing items for this file to avoid duplicates
        ReceiptItem.objects.filter(session=session, source_file=extracted_file).delete()
        
        # Create new ReceiptItem objects
        for item_data in filtered_data:
            ReceiptItem.objects.create(
                session=session,
                source_file=extracted_file,
                item_name=item_data['item'],
                price=Decimal(str(item_data['price'])),
                is_confirmed=False  # Not confirmed yet
            )
        
        logger.info(f"Saved {len(filtered_data)} items for {file}")
        
        return JsonResponse({
            'success': True,
            'message': f'Saved {len(filtered_data)} items for {file}'
        })
        
    except Exception as e:
        logger.error(f"Error saving extraction for {file}: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return JsonResponse({'error': f'Save failed: {str(e)}'}, status=500)

@login_required
@require_POST
def confirm_extraction(request):
    """Confirm and finalize the extraction data, storing it in database."""
    try:
        session = get_or_create_session(request.user)
        extracted_data_json = request.POST.get('extracted_data')
        selected_file = request.POST.get('selected_file')
        
        logger.debug(f"Confirming extraction for file: {selected_file}")
        logger.debug(f"Data length: {len(extracted_data_json) if extracted_data_json else 0}")
        
        if not extracted_data_json or not selected_file:
            return JsonResponse({'error': 'Missing extracted_data or selected_file'}, status=400)
        
        # Validate that the file belongs to this user's session
        extracted_file = session.extracted_files.filter(filename=selected_file).first()
        if not extracted_file:
            return JsonResponse({'error': 'File not found or access denied'}, status=404)
        
        # Parse the JSON data
        extracted_data = json.loads(extracted_data_json)
        logger.debug(f"Parsed {len(extracted_data)} items from JSON")
        
        # Validate the data structure
        if not isinstance(extracted_data, list):
            return JsonResponse({'error': 'Invalid data format: expected list'}, status=400)
        
        # Validate each item in the list
        for item in extracted_data:
            if not isinstance(item, dict) or 'item' not in item or 'price' not in item:
                return JsonResponse({'error': 'Invalid item format: expected {"item": "", "price": ""}'}, status=400)
        
        # Delete any existing items for this file to avoid duplicates
        ReceiptItem.objects.filter(session=session, source_file=extracted_file).delete()
        
        # Create new ReceiptItem objects
        receipt_items = []
        for item_data in extracted_data:
            receipt_item = ReceiptItem(
                session=session,
                source_file=extracted_file,
                item_name=item_data['item'],
                price=Decimal(str(item_data['price'])),
                is_confirmed=True  # Mark as confirmed
            )
            receipt_items.append(receipt_item)
        
        # Bulk create for efficiency
        ReceiptItem.objects.bulk_create(receipt_items)
        
        # Mark the extracted file as processed
        extracted_file.is_processed = True
        extracted_file.extracted_at = timezone.now()
        extracted_file.save()
        
        logger.info(f"Confirmed extraction: {len(extracted_data)} items for {selected_file}")
        logger.debug(f"Data structure - Type: {type(extracted_data)}, Length: {len(extracted_data)}")
        if extracted_data:
            logger.debug(f"First item: {extracted_data[0]}")
        
        # Return JSON like the old version
        return JsonResponse({
            'success': True,
            'item_count': len(extracted_data),
            'message': f'Successfully stored {len(extracted_data)} items for {selected_file}'
        })
        
    except Exception as e:
        error_details = traceback.format_exc()
        logger.error(f"Confirmation failed for {selected_file}: {str(e)}")
        logger.error(f"TRACEBACK: {error_details}")
        return JsonResponse({'error': f'Confirmation failed: {str(e)}'}, status=500)

@login_required
@require_POST
def clear_selection(request):
    """Clear the selected file and return to initial extraction state."""
    try:
        session = get_or_create_session(request.user)
        
        # Clear the selected file from Django session
        if 'selected_file' in request.session:
            del request.session['selected_file']
        
        logger.debug("Cleared selected file from Django session")
        
        # Check if all files have been processed
        unprocessed_files = session.extracted_files.filter(is_processed=False, is_skipped=False)
        confirmed_items = session.receipt_items.filter(is_confirmed=True)
        
        logger.debug(f"Remaining unprocessed files: {unprocessed_files.count()}")
        logger.debug(f"Confirmed items: {confirmed_items.count()}")
        
        if not unprocessed_files.exists() and confirmed_items.exists():
            # All files have been processed, move to sorting step
            session.current_step = 3  # Move to Sort step
            session.save()
            logger.info("All files processed, advancing to Sort step")
        
        # Get updated file lists
        unprocessed_files = session.extracted_files.filter(is_processed=False, is_skipped=False)
        
        # Return the full page template with updated state
        return render(request, 'start_page.html', {
            'current_step': session.current_step,
            'extracted_files': list(unprocessed_files.values_list('filename', flat=True)),
            'session': session,
            'state': {
                'current_step': session.current_step,
                'receipt_zip': session.receipt_zip_filename,
                'payer': session.payer,
                'api_costs_total': float(session.api_costs_total),
                'current_extraction_index': session.current_extraction_index,
                'files_processed': session.files_processed,
                'progress_percentage': session.progress_percentage,
                'current_sort_index': session.current_sort_index,
                'extracted_files': list(unprocessed_files.values_list('filename', flat=True)),
                'consumption': get_consumption_data(session) if session.current_step >= 3 else {},
                'sort_items': get_sort_items(session) if session.current_step == 3 else [],
                'aggregation': get_aggregation_data(session) if session.current_step == 4 else {},
            }
        })
        
    except Exception as e:
        error_details = traceback.format_exc()
        logger.error(f"Clear selection failed: {str(e)}")
        logger.error(f"TRACEBACK: {error_details}")
        return HttpResponse(f'<div class="alert alert-error">Clear selection failed: {str(e)}</div>', status=500)

@login_required
@require_POST
def assign_item(request):
    """Assign current item to a person (sebastian, iva, or both)."""
    try:
        session = get_or_create_session(request.user)
        assignee = request.POST.get('assignee')
        
        if assignee not in ['sebastian', 'iva', 'both']:
            return JsonResponse({'error': 'Invalid assignee'}, status=400)
        
        # Get the next unassigned confirmed receipt item
        unassigned_items = session.receipt_items.filter(
            is_confirmed=True,
            sorted_assignment__isnull=True
        ).order_by('id')
        
        current_item = unassigned_items.first()
        
        if not current_item:
            return JsonResponse({'error': 'No more items to sort'}, status=400)
        
        # Create SortedItem assignment
        SortedItem.objects.create(
            session=session,
            receipt_item=current_item,
            assignee=assignee
        )
        
        # Update current sort index
        session.current_sort_index += 1
        session.save()
        
        logger.info(f"Assigned '{current_item.item_name}' (CHF {current_item.price}) to {assignee}")
        
        # Check if we're done sorting
        remaining_unassigned = session.receipt_items.filter(
            is_confirmed=True,
            sorted_assignment__isnull=True
        ).count()
        
        logger.debug(f"Remaining unassigned items: {remaining_unassigned}")
        
        if remaining_unassigned == 0:
            # All items sorted - move directly to aggregation
            session.current_step = 4
            session.save()
            calculate_aggregation(session)
            logger.info("All items sorted, advancing to Aggregation step")
            
            # Return just the aggregate template content with trigger to update sidebar
            response = render(request, '5_aggregate.html', {
                'state': {
                    'current_step': session.current_step,
                    'receipt_zip': session.receipt_zip_filename,
                    'payer': session.payer,
                    'api_costs_total': float(session.api_costs_total),
                    'current_extraction_index': session.current_extraction_index,
                    'files_processed': session.files_processed,
                    'progress_percentage': session.progress_percentage,
                    'current_sort_index': session.current_sort_index,
                    'consumption': get_consumption_data(session),
                    'sort_items': get_sort_items(session),
                    'aggregation': get_aggregation_data(session),
                }
            })
            response['HX-Trigger'] = 'sortingComplete'
            return response
        
        # Return the updated sort template with next item
        return render(request, '4_sort.html', {
            'state': {
                'current_step': session.current_step,
                'receipt_zip': session.receipt_zip_filename,
                'payer': session.payer,
                'api_costs_total': float(session.api_costs_total),
                'current_extraction_index': session.current_extraction_index,
                'files_processed': session.files_processed,
                'progress_percentage': session.progress_percentage,
                'current_sort_index': session.current_sort_index,
                'consumption': get_consumption_data(session),
                'sort_items': get_sort_items(session),
                'aggregation': get_aggregation_data(session),
            }
        })
        
    except Exception as e:
        error_details = traceback.format_exc()
        logger.error(f"Item assignment failed: {str(e)}")
        logger.error(f"TRACEBACK: {error_details}")
        return JsonResponse({'error': f'Assignment failed: {str(e)}'}, status=500)

@login_required
@require_GET
def get_current_sort_item(request):
    """Get the current item to be sorted and return HTML."""
    try:
        session = get_or_create_session(request.user)
        logger.debug("Getting current sort item")
        logger.debug(f"Current sort index: {session.current_sort_index}")
        
        # Get the next unassigned confirmed receipt item
        unassigned_items = session.receipt_items.filter(
            is_confirmed=True,
            sorted_assignment__isnull=True
        ).order_by('id')
        
        total_items = session.receipt_items.filter(is_confirmed=True).count()
        assigned_items = session.sorted_items.count()
        current_index = assigned_items
        
        logger.debug(f"Current index: {current_index}, Total items: {total_items}")
        
        current_item = unassigned_items.first()
        
        if not current_item:
            # All items sorted - move directly to aggregation
            session.current_step = 4
            session.save()
            calculate_aggregation(session)
            logger.info("All items sorted, showing aggregation results")
            
            # Return just the aggregate template content with trigger to update sidebar
            response = render(request, '5_aggregate.html', {
                'state': {
                    'current_step': session.current_step,
                    'receipt_zip': session.receipt_zip_filename,
                    'payer': session.payer,
                    'api_costs_total': float(session.api_costs_total),
                    'current_extraction_index': session.current_extraction_index,
                    'files_processed': session.files_processed,
                    'progress_percentage': session.progress_percentage,
                    'current_sort_index': session.current_sort_index,
                    'consumption': get_consumption_data(session),
                    'sort_items': get_sort_items(session),
                    'aggregation': get_aggregation_data(session),
                }
            })
            response['HX-Trigger'] = 'sortingComplete'
            return response
        
        logger.debug(f"Displaying item: {current_item.item_name} (CHF {current_item.price})")
        
        # Calculate progress percentage
        progress_percentage = int((current_index / total_items) * 100) if total_items > 0 else 0
        
        # Return HTML for current item and progress update
        progress_text = f"Item {current_index + 1} of {total_items}"
        
        return HttpResponse(f"""
            <div class="space-y-4">
                <h3 class="text-xl font-bold text-base-content">{current_item.item_name}</h3>
                <p class="text-2xl font-mono text-primary">CHF {current_item.price}</p>
                <p class="text-sm text-base-content/60">From: {current_item.source_file.filename}</p>
            </div>
            <script>
                // Update progress bar and numbers only
                document.getElementById('sorting-progress').value = {progress_percentage};
                document.getElementById('current-item-number').textContent = '{current_index + 1}';
                document.getElementById('total-items-number').textContent = '{total_items}';
                
                // Enable buttons
                document.getElementById('sebastian-btn').disabled = false;
                document.getElementById('iva-btn').disabled = false;
                document.getElementById('both-btn').disabled = false;
            </script>
        """)
        
    except Exception as e:
        error_details = traceback.format_exc()
        logger.error(f"Failed to get current sort item: {str(e)}")
        logger.error(f"TRACEBACK: {error_details}")
        return HttpResponse(f"""
            <div class="space-y-4">
                <span class="material-symbols-rounded text-6xl text-error">error</span>
                <h3 class="text-xl font-bold text-error">Failed to load current item</h3>
                <p class="text-base-content/60">{str(e)}</p>
            </div>
        """)

def calculate_aggregation(session):
    """Calculate spending aggregation and transfer payment."""
    try:
        payer = session.payer
        
        logger.debug(f"Calculating aggregation for payer: {payer}")
        
        # Get consumption data from sorted items
        sebastian_items = session.sorted_items.filter(assignee='sebastian').select_related('receipt_item')
        iva_items = session.sorted_items.filter(assignee='iva').select_related('receipt_item')
        both_items = session.sorted_items.filter(assignee='both').select_related('receipt_item')
        
        logger.debug(f"Items count - Sebastian: {sebastian_items.count()}, Iva: {iva_items.count()}, Both: {both_items.count()}")
        
        # Calculate sums for each category
        sebastian_total = sum(item.receipt_item.price for item in sebastian_items)
        iva_total = sum(item.receipt_item.price for item in iva_items)
        both_total = sum(item.receipt_item.price for item in both_items)
        
        logger.debug(f"Raw totals - Sebastian: {sebastian_total}, Iva: {iva_total}, Both: {both_total}")
        
        # Calculate transfer payment based on payer
        if payer and payer.lower() == 'iva':
            # Sebastian owes Iva: Sebastian's expenses + half of shared expenses
            transfer_amount = sebastian_total + (both_total / 2)
            transfer_direction = f"Sebastian → Iva"
        elif payer and payer.lower() == 'sebastian':
            # Iva owes Sebastian: Iva's expenses + half of shared expenses
            transfer_amount = iva_total + (both_total / 2)
            transfer_direction = f"Iva → Sebastian"
        else:
            transfer_amount = Decimal('0')
            transfer_direction = "No payer specified"
        
        grand_total = sebastian_total + iva_total + both_total
        
        # Create or update SessionAggregation
        aggregation, created = SessionAggregation.objects.get_or_create(
            session=session,
            defaults={
            'sebastian_total': sebastian_total,
            'iva_total': iva_total,
            'both_total': both_total,
                'grand_total': grand_total,
            'transfer_amount': transfer_amount,
            'transfer_direction': transfer_direction,
            }
        )
        
        if not created:
            # Update existing aggregation
            aggregation.sebastian_total = sebastian_total
            aggregation.iva_total = iva_total
            aggregation.both_total = both_total
            aggregation.grand_total = grand_total
            aggregation.transfer_amount = transfer_amount
            aggregation.transfer_direction = transfer_direction
            aggregation.calculated_at = timezone.now()
            aggregation.save()
        
        logger.info(f"Aggregation calculated: Total CHF {grand_total:.2f}")
        logger.info(f"Transfer payment: {transfer_direction} CHF {transfer_amount:.2f}")
        logger.debug(f"Breakdown - Sebastian: CHF {sebastian_total:.2f}, Iva: CHF {iva_total:.2f}, Shared: CHF {both_total:.2f}")
        
        return {
            'sebastian_total': float(sebastian_total),
            'iva_total': float(iva_total),
            'both_total': float(both_total),
            'transfer_amount': float(transfer_amount),
            'transfer_direction': transfer_direction,
            'payer': payer,
            'grand_total': float(grand_total)
        }
        
    except Exception as e:
        logger.error(f"Error calculating aggregation: {str(e)}")
        return {
            'sebastian_total': 0,
            'iva_total': 0,
            'both_total': 0,
            'transfer_amount': 0,
            'transfer_direction': 'Error calculating',
            'payer': 'Unknown',
            'grand_total': 0
        }

@login_required
@require_POST
def start_extraction(request):
    """Initialize the extraction process and set the first file as current."""
    try:
        session = get_or_create_session(request.user)
        
        # Get unprocessed files
        unprocessed_files = session.extracted_files.filter(is_processed=False, is_skipped=False)
        
        if not unprocessed_files.exists():
            return HttpResponse('<div class="alert alert-error">No files to extract</div>', status=400)
        
        # Reset extraction progress
        session.current_extraction_index = 0
        session.files_processed = 0
        session.progress_percentage = 0
        session.save()
        
        total_files = unprocessed_files.count()
        first_file_obj = unprocessed_files.first()
        first_file = first_file_obj.filename
        
        logger.info(f"Started extraction process with {total_files} files")
        logger.info(f"First file: {first_file}")
        
        # Store current file in Django session
        request.session['current_file'] = first_file
        
        # Get updated unprocessed files for context
        unprocessed_files_list = list(unprocessed_files.values_list('filename', flat=True))
        
        # Create context that matches what start_page.html expects
        context = {
            'current_step': session.current_step,
            'extracted_files': unprocessed_files_list,
            'session': session,
            'current_file': first_file,  # Original filename for display
            'state': {
                'current_step': session.current_step,
                'receipt_zip': session.receipt_zip_filename,
                'payer': session.payer,
                'api_costs_total': float(session.api_costs_total),
                'current_extraction_index': session.current_extraction_index,
                'files_processed': session.files_processed,
                'progress_percentage': session.progress_percentage,
                'current_sort_index': session.current_sort_index,
                'extracted_files': unprocessed_files_list,
                'current_file': first_file,  # This is what the template needs
                'consumption': get_consumption_data(session) if session.current_step >= 3 else {},
                'sort_items': get_sort_items(session) if session.current_step == 3 else [],
                'aggregation': get_aggregation_data(session) if session.current_step == 4 else {},
            }
        }
        
        # Return just the extraction template instead of full page
        return render(request, '3_extract_receipts.html', {
            'current_file': first_file,
            'total_files': total_files,
            'files_processed': 0,
            'progress_percentage': 0
        })
        
    except Exception as e:
        logger.error(f"Failed to start extraction: {str(e)}")
        return HttpResponse(f'<div class="alert alert-error">Failed to start extraction: {str(e)}</div>', status=500)

@login_required
@require_POST
def extract_current_image(request):
    """Extract receipt data from the current image in the sequence."""
    try:
        session = get_or_create_session(request.user)
        current_file = request.session.get('current_file')
        
        if not current_file:
            logger.error("No current file set for extraction")
            return HttpResponse('<div class="alert alert-error">No current file set</div>', status=400)
        
        logger.info(f"Extracting data from current file: {current_file}")
        
        # Validate that the current file belongs to this user's session
        extracted_file = session.extracted_files.filter(filename=current_file).first()
        if not extracted_file:
            logger.error(f"File {current_file} not found in user's session")
            return HttpResponse('<div class="alert alert-error">File not found or access denied</div>', status=404)
        
        # Check if we have the receipt_zip in session
        if not session.receipt_zip_filename:
            logger.error("No receipt ZIP file found in session")
            return HttpResponse('<div class="alert alert-error">No receipt ZIP file found in session</div>', status=400)
        
        # Get the image path
        zip_filename = session.receipt_zip_filename
        extract_dir_name = Path(zip_filename).stem
        image_path = Path(settings.BASE_DIR) / 'data' / '1_unzipped' / extract_dir_name / extracted_file.relative_path
        
        if not image_path.exists():
            logger.error(f"Image file not found at: {image_path}")
            return HttpResponse(f'<div class="alert alert-error">Image file not found at: {image_path}</div>', status=404)
        
        # Get OpenAI API key from environment
        try:
            # Ensure .env is loaded
            load_dotenv(Path(settings.BASE_DIR) / 'config' / '.env')
            openai_api_key = os.environ['OPENAI_API_KEY']
            logger.debug(f"OpenAI API key loaded: {bool(openai_api_key)}")
        except KeyError:
            logger.error("OpenAI API key not configured")
            return HttpResponse('<div class="alert alert-error">OpenAI API key not configured</div>', status=500)
        
        # Extract data from image
        extracted_data, cost = image_to_dataframe_dict(image_path, openai_api_key)
        logger.info(f"Extraction completed. Found {len(extracted_data)} items, cost: ${cost:.4f}")
        
        # Add cost to total API costs
        session.api_costs_total += Decimal(str(cost))
        session.save()
        logger.info(f"Total API costs now: ${session.api_costs_total:.4f}")
        
        # Store extracted data in Django session for later use
        request.session['extracted_data'] = extracted_data
        
        # Return the table template with next file button
        return render(request, 'extracted_data_table.html', {
            'extracted_data': extracted_data,
            'cost': cost,
            'selected_file': current_file,
            'current_file': current_file,
            'show_next_button': True
        })
        
    except Exception as e:
        error_details = traceback.format_exc()
        logger.error(f"Extraction failed for {current_file}: {str(e)}")
        logger.error(f"TRACEBACK: {error_details}")
        return HttpResponse(f'<div class="alert alert-error">Extraction failed: {str(e)}</div>', status=500)

@login_required
@require_POST
def skip_current_file(request):
    """Skip the current file and move to the next one."""
    try:
        session = get_or_create_session(request.user)
        current_file = request.session.get('current_file')
        
        if current_file:
            logger.info(f"Skipping file: {current_file}")
            
            # Mark the file as skipped in the database
            extracted_file = session.extracted_files.filter(filename=current_file).first()
            if extracted_file:
                extracted_file.is_skipped = True
                extracted_file.save()
                logger.info(f"Marked {current_file} as skipped")
        
        # Use the new targeted content system to move to next file
        return next_extraction_content(request)
        
    except Exception as e:
        logger.error(f"Failed to skip current file: {str(e)}")
        return HttpResponse(f'<div class="alert alert-error">Failed to skip file: {str(e)}</div>', status=500)

@login_required
@require_POST  
def next_file(request):
    """Move to the next file in the extraction sequence."""
    try:
        # Get current progress
        current_index = request.session.get('current_extraction_index', 0)
        extracted_files = request.session.get('extracted_files', [])
        
        # Move to next file
        current_index += 1
        request.session['current_extraction_index'] = current_index
        
        # Calculate progress
        total_files = len(extracted_files)
        files_processed = current_index
        progress_percentage = int((files_processed / total_files) * 100) if total_files > 0 else 0
        
        request.session['files_processed'] = files_processed
        request.session['progress_percentage'] = progress_percentage
        
        if current_index < len(extracted_files):
            # Set next file as current
            request.session['current_file'] = extracted_files[current_index]
            logger.info(f"Moving to next file: {request.session['current_file']} ({current_index + 1}/{len(extracted_files)})")
        else:
            # All files processed
            request.session['current_file'] = None
            session = get_or_create_session(request.user)
            session.current_step = 3  # Move to Sort step
            session.save()
            logger.info("All files processed, advancing to Sort step")
            
            # Prepare sort items from all extracted data
            if request.session.get('extracted', {}):
                sort_items = []
                for filename, data in request.session['extracted'].items():
                    for item in data:
                        sort_items.append({
                            'item': item['item'],
                            'price': float(item['price']),
                            'filename': filename
                        })
                request.session['sort_items'] = sort_items
                session.sort_items = sort_items
                session.current_sort_index = 0
                session.save()
                logger.info(f"Prepared {len(sort_items)} items for sorting")
        
        # Return the full page with updated state
        return render(request, 'start_page.html', {
            'current_step': session.current_step,
            'extracted_files': request.session.get('extracted_files', []),
            'state': request.session
        })
        
    except Exception as e:
        logger.error(f"Failed to advance to next file: {str(e)}")
        return HttpResponse(f'<div class="alert alert-error">Failed to advance: {str(e)}</div>', status=500)

@login_required
@require_POST  
def next_file_in_queue(request):
    """Move to the next unprocessed file in the extraction queue."""
    try:
        session = get_or_create_session(request.user)
        
        # Get all unprocessed files
        unprocessed_files = session.extracted_files.filter(is_processed=False, is_skipped=False).order_by('filename')
        
        # Update progress tracking
        total_files = session.extracted_files.count()
        processed_files = session.extracted_files.filter(is_processed=True).count()
        skipped_files = session.extracted_files.filter(is_skipped=True).count()
        files_processed = processed_files + skipped_files
        progress_percentage = int((files_processed / total_files) * 100) if total_files > 0 else 0
        
        session.current_extraction_index = files_processed
        session.files_processed = files_processed
        session.progress_percentage = progress_percentage
        session.save()
        
        logger.info(f"Progress update: {files_processed}/{total_files} files completed ({progress_percentage}%)")
        
        if unprocessed_files.exists():
            # Set next file as current
            next_file = unprocessed_files.first()
            request.session['current_file'] = next_file.filename
            logger.info(f"Moving to next file: {next_file.filename}")
            
            # Use the targeted content function instead of full page render
            return next_extraction_content(request)
        else:
            # All files processed - check if we have any confirmed items to move to sorting
            confirmed_items = session.receipt_items.filter(is_confirmed=True)
            
            if confirmed_items.exists():
                # Move to sorting step
                session.current_step = 3
                session.save()
                logger.info("All files processed, advancing to Sort step")
                
                # Clear current file
                if 'current_file' in request.session:
                    del request.session['current_file']
                
                # Return sorting template with trigger to update progress steps
                response = render(request, '4_sort.html', {
                    'state': {
                        'current_step': session.current_step,
                        'receipt_zip': session.receipt_zip_filename,
                        'payer': session.payer,
                        'api_costs_total': float(session.api_costs_total),
                        'current_extraction_index': session.current_extraction_index,
                        'files_processed': session.files_processed,
                        'progress_percentage': session.progress_percentage,
                        'current_sort_index': session.current_sort_index,
                        'consumption': get_consumption_data(session),
                        'sort_items': get_sort_items(session),
                        'aggregation': get_aggregation_data(session),
                    }
                })
                
                # Add success toast for completion
                completion_toast = '''
                    <div class="toast toast-top toast-center" id="completion-toast" hx-swap-oob="true">
                        <div class="alert alert-success">
                            <span>🎉 All files processed! Now sort the extracted items.</span>
                        </div>
                    </div>
                    <script>
                        setTimeout(() => {
                            const toast = document.getElementById('completion-toast');
                            if (toast) toast.remove();
                        }, 5000);
                    </script>
                '''
                
                # Combine content
                full_content = response.content.decode('utf-8') + completion_toast
                response = HttpResponse(full_content)
                
                # Add trigger to update the progress steps
                response['HX-Trigger'] = 'extractionComplete'
                return response
            else:
                # No confirmed items - advance to final step with zeros and warning
                logger.warning("All files processed but no confirmed items found")
                session.current_step = 4  # Move to aggregation step
                session.save()
                
                # Calculate aggregation even with no items to create proper zero-valued record
                calculate_aggregation(session)
                
                # Clear current file
                if 'current_file' in request.session:
                    del request.session['current_file']
                
                # Return aggregation template with warning and zero amounts
                response = render(request, '5_aggregate.html', {
                    'state': {
                        'current_step': session.current_step,
                        'receipt_zip': session.receipt_zip_filename,
                        'payer': session.payer,
                        'api_costs_total': float(session.api_costs_total),
                        'current_extraction_index': session.current_extraction_index,
                        'files_processed': session.files_processed,
                        'progress_percentage': session.progress_percentage,
                        'current_sort_index': session.current_sort_index,
                        'consumption': get_consumption_data(session),
                        'sort_items': get_sort_items(session),
                        'aggregation': get_aggregation_data(session),
                    }
                })
                
                # Add warning toast
                warning_toast = '''
                    <div class="toast toast-top toast-center" id="warning-toast" hx-swap-oob="true">
                        <div class="alert alert-warning">
                            <span>⚠️ All files processed but no items were confirmed for sorting.</span>
                        </div>
                    </div>
                    <script>
                        setTimeout(() => {
                            const toast = document.getElementById('warning-toast');
                            if (toast) toast.remove();
                        }, 8000);
                    </script>
                '''
                
                # Combine content
                full_content = response.content.decode('utf-8') + warning_toast
                response = HttpResponse(full_content)
                
                # Add trigger to update the progress steps to final step
                response['HX-Trigger'] = 'sortingComplete'
                return response
        
    except Exception as e:
        logger.error(f"Failed to get next extraction content: {str(e)}")
        return HttpResponse(f'<div class="alert alert-error">Failed to advance: {str(e)}</div>', status=500)

@login_required
@require_GET
def get_progress_update(request):
    """Return just the progress section for targeted updates."""
    try:
        session = get_or_create_session(request.user)
        current_file = request.session.get('current_file')
        
        # Update progress tracking
        total_files = session.extracted_files.count()
        processed_files = session.extracted_files.filter(is_processed=True).count()
        skipped_files = session.extracted_files.filter(is_skipped=True).count()
        files_processed = processed_files + skipped_files
        progress_percentage = int((files_processed / total_files) * 100) if total_files > 0 else 0
        
        # Return just the progress HTML fragment
        return HttpResponse(f'''
            <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
                <div class="flex-1">
                    <h3 class="text-lg font-semibold text-base-content mb-2">Extraction Progress</h3>
                    {"<p class=\"text-sm text-base-content/70\">Currently extracting: <span class=\"font-mono\">" + current_file + "</span></p>" if current_file else "<p class=\"text-sm text-base-content/70\">Ready to start extraction</p>"}
                </div>
                <div class="text-right">
                    <div class="text-sm text-base-content/60 mb-1">
                        {files_processed} of {total_files} files completed
                    </div>
                    <div class="w-48">
                        <progress class="progress progress-primary w-full" value="{progress_percentage}" max="100"></progress>
                    </div>
                </div>
            </div>
        ''')
        
    except Exception as e:
        logger.error(f"Failed to get progress update: {str(e)}")
        return HttpResponse('<div class="alert alert-error">Progress update failed</div>', status=500)

@login_required
@require_POST  
def next_extraction_content(request):
    """Return just the extraction content for the next file (for targeted HTMX swap)."""
    try:
        session = get_or_create_session(request.user)
        
        # Get all unprocessed files
        unprocessed_files = session.extracted_files.filter(is_processed=False, is_skipped=False).order_by('filename')
        
        # Update progress tracking
        total_files = session.extracted_files.count()
        processed_files = session.extracted_files.filter(is_processed=True).count()
        skipped_files = session.extracted_files.filter(is_skipped=True).count()
        files_processed = processed_files + skipped_files
        progress_percentage = int((files_processed / total_files) * 100) if total_files > 0 else 0
        
        session.current_extraction_index = files_processed
        session.files_processed = files_processed
        session.progress_percentage = progress_percentage
        session.save()
        
        logger.info(f"Progress update: {files_processed}/{total_files} files completed ({progress_percentage}%)")
        
        if unprocessed_files.exists():
            # Set next file as current
            next_file = unprocessed_files.first()
            request.session['current_file'] = next_file.filename
            logger.info(f"Moving to next file: {next_file.filename}")
            
            # Return just the extraction template with the next file and OOB progress update
            extraction_content = render(request, '3_extract_receipts.html', {
                'current_file': next_file.filename,
                'total_files': total_files,
                'files_processed': files_processed,
                'progress_percentage': progress_percentage
            })
            
            # Add Out-of-Band progress update and success toast
            progress_html = f'''
                <div id="progress-content" class="flex flex-col sm:flex-row sm:items-center justify-between gap-4" hx-swap-oob="true">
                    <div class="flex-1">
                        <h3 class="text-lg font-semibold text-base-content mb-2">Extraction Progress</h3>
                        <p class="text-sm text-base-content/70">Currently extracting: <span class="font-mono">{next_file.filename}</span></p>
                    </div>
                    <div class="text-right">
                        <div class="text-sm text-base-content/60 mb-1">
                            {files_processed} of {total_files} files completed
                        </div>
                        <div class="w-48">
                            <progress class="progress progress-primary w-full" value="{progress_percentage}" max="100"></progress>
                        </div>
                    </div>
                </div>
                
                <div class="toast toast-top toast-center" id="success-toast" hx-swap-oob="true">
                    <div class="alert alert-success">
                        <span>✅ Extraction confirmed! Moving to next file...</span>
                    </div>
                </div>
                <script>
                    setTimeout(() => {{
                        const toast = document.getElementById('success-toast');
                        if (toast) toast.remove();
                    }}, 3000);
                </script>
            '''
            
            # Combine the content
            full_content = extraction_content.content.decode('utf-8') + progress_html
            return HttpResponse(full_content)
        else:
            # All files processed - return sorting step content
            confirmed_items = session.receipt_items.filter(is_confirmed=True)
            
            if confirmed_items.exists():
                # Move to sorting step
                session.current_step = 3
                session.save()
                logger.info("All files processed, advancing to Sort step")
                
                # Clear current file
                if 'current_file' in request.session:
                    del request.session['current_file']
                
                # Return sorting template with trigger to update progress steps
                response = render(request, '4_sort.html', {
                    'state': {
                        'current_step': session.current_step,
                        'receipt_zip': session.receipt_zip_filename,
                        'payer': session.payer,
                        'api_costs_total': float(session.api_costs_total),
                        'current_extraction_index': session.current_extraction_index,
                        'files_processed': session.files_processed,
                        'progress_percentage': session.progress_percentage,
                        'current_sort_index': session.current_sort_index,
                        'consumption': get_consumption_data(session),
                        'sort_items': get_sort_items(session),
                        'aggregation': get_aggregation_data(session),
                    }
                })
                
                # Add success toast for completion
                completion_toast = '''
                    <div class="toast toast-top toast-center" id="completion-toast" hx-swap-oob="true">
                        <div class="alert alert-success">
                            <span>🎉 All files processed! Now sort the extracted items.</span>
                        </div>
                    </div>
                    <script>
                        setTimeout(() => {
                            const toast = document.getElementById('completion-toast');
                            if (toast) toast.remove();
                        }, 5000);
                    </script>
                '''
                
                # Combine content
                full_content = response.content.decode('utf-8') + completion_toast
                response = HttpResponse(full_content)
                
                # Add trigger to update the progress steps to final step
                response['HX-Trigger'] = 'sortingComplete'
                return response
            else:
                # No confirmed items - this shouldn't happen
                logger.warning("All files processed but no confirmed items found")
                session.current_step = 4  # Move to aggregation step
                session.save()
                
                # Calculate aggregation even with no items to create proper zero-valued record
                calculate_aggregation(session)
                
                # Clear current file
                if 'current_file' in request.session:
                    del request.session['current_file']
                
                # Return aggregation template with warning and zero amounts
                response = render(request, '5_aggregate.html', {
                    'state': {
                        'current_step': session.current_step,
                        'receipt_zip': session.receipt_zip_filename,
                        'payer': session.payer,
                        'api_costs_total': float(session.api_costs_total),
                        'current_extraction_index': session.current_extraction_index,
                        'files_processed': session.files_processed,
                        'progress_percentage': session.progress_percentage,
                        'current_sort_index': session.current_sort_index,
                        'consumption': get_consumption_data(session),
                        'sort_items': get_sort_items(session),
                        'aggregation': get_aggregation_data(session),
                    }
                })
                
                # Add warning toast
                warning_toast = '''
                    <div class="toast toast-top toast-center" id="warning-toast" hx-swap-oob="true">
                        <div class="alert alert-warning">
                            <span>⚠️ All files processed but no items were confirmed for sorting.</span>
                        </div>
                    </div>
                    <script>
                        setTimeout(() => {
                            const toast = document.getElementById('warning-toast');
                            if (toast) toast.remove();
                        }, 8000);
                    </script>
                '''
                
                # Combine content
                full_content = response.content.decode('utf-8') + warning_toast
                response = HttpResponse(full_content)
                
                # Add trigger to update the progress steps to final step
                response['HX-Trigger'] = 'sortingComplete'
                return response
        
    except Exception as e:
        logger.error(f"Failed to get next extraction content: {str(e)}")
        return HttpResponse(f'<div class="alert alert-error">Failed to advance: {str(e)}</div>', status=500)

@login_required
@require_GET
def authelia_logout(request):
    """Logout from Django and redirect to Authelia logout."""
    from django.contrib.auth import logout
    from django.shortcuts import redirect
    from django.conf import settings
    
    logout(request)
    authelia_logout_url = getattr(settings, 'AUTHELIA_LOGOUT_URL', '/accounts/login/')
    return redirect(authelia_logout_url)

@require_GET
def custom_login(request):
    """Custom login view that provides Authelia context."""
    # If user is already authenticated, redirect to home
    if request.user.is_authenticated:
        return redirect('/')
    
    context = {
        'authelia_app_url': settings.AUTHELIA_APP_URL,
    }
    
    return render(request, 'registration/login.html', context)
