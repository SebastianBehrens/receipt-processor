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
from django.shortcuts import render, redirect
from django.http import HttpResponse, FileResponse, JsonResponse
from django.conf import settings
from django.views.decorators.http import require_GET, require_POST
from django.utils.text import get_valid_filename
from django.contrib.auth.decorators import login_required
from dotenv import load_dotenv

from .utils import (
    get_user_state, require_auth, get_files_by_status, get_next_tbd_file,
    update_file_status, get_total_files, get_processed_files_count,
    ensure_consumption_initialized
)

# Load environment variables from config/.env
load_dotenv(Path(settings.BASE_DIR) / 'config' / '.env')

# Set up logging
logger = logging.getLogger(__name__)

def unzip_receipts(zip_filename):
    """Extract the uploaded ZIP file and return list of extracted files."""
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
                    extracted_files.append({"file": str(relative_path), "status": "tbd"})
    
    except Exception as e:
        logger.error(f"Error extracting ZIP file: {e}")
        return []
    
    return extracted_files


def health(request):
    """Unauthenticated health check for Swarm/Traefik probes."""
    return HttpResponse("ok", content_type="text/plain")


@require_auth
def start_page(request):
    """Render the start page for the HTMX Receipt Processor."""
    user_state = request.user_state
    return render(request, 'start_page.html', {
        'current_step': user_state.get('current_step', 0),
        'extracted_files': user_state.get('extracted_files', []),
        'state': user_state.get_state(),
        'user': request.user
    })

@require_POST
@require_auth
def step_view(request, step_number):
    """Main view to handle different steps of the receipt processing workflow."""
    logger.info(f"Step view requested: {step_number} for user {request.user.username}")
    user_state = request.user_state
    
    # Update current step (convert to 0-based index for internal use)
    user_state['current_step'] = step_number - 1
    
    # If moving to aggregation step (step 5 = index 4), calculate aggregation
    if step_number == 5:
        calculate_aggregation(user_state)
    
    # If moving to sort step (step 4 = index 3), initialize sorting if needed
    if step_number == 4:
        # Initialize consumption tracking if not exists
        ensure_consumption_initialized(user_state)
        
        # Initialize sort items if not exists but we have extracted data
        if not user_state.get('sort_items') and user_state.get('extracted', {}):
            # Aggregate all items from all files into a single list
            all_items = []
            for filename, items in user_state.get('extracted', {}).items():
                for item in items:
                    # Add filename for reference
                    item_with_source = dict(item)
                    item_with_source['source_file'] = filename
                    all_items.append(item_with_source)
            
            user_state['sort_items'] = all_items
            user_state['current_sort_index'] = 0
            logger.info(f"Auto-initialized sorting with {len(all_items)} items for step navigation")
        
        # If there are no items to sort, initialize empty sort_items
        if not user_state.get('sort_items'):
            user_state['sort_items'] = []
            user_state['current_sort_index'] = 0
            logger.info("No items to sort, initialized empty sort_items")
    
    # Calculate progress information for extraction step
    if step_number == 3 and user_state.get('extracted_files'):  # Step 3 is extraction (0-based index 2)
        total_files = get_total_files(user_state)
        files_processed = get_processed_files_count(user_state)
        progress_percentage = int((files_processed / total_files) * 100) if total_files > 0 else 0
        
        user_state['files_processed'] = files_processed
        user_state['progress_percentage'] = progress_percentage
        
        # Set current file to next TBD file
        next_file = get_next_tbd_file(user_state)
        user_state['current_file'] = next_file['file'] if next_file else None
        
        logger.debug(f"Extraction progress: {files_processed}/{total_files} ({progress_percentage}%)")
    
    # Create context with state information
    context = {
        'current_step': user_state.get('current_step', 0),
        'extracted_files': user_state.get('extracted_files', []),
        'state': user_state.get_state(),
        'user': request.user
    }
    
    logger.debug(f"Rendering step {step_number} with context keys: {list(context.keys())}")
    return render(request, 'start_page.html', context)

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
        
        # Store filename and payer in state
        user_state = request.user_state
        user_state['receipt_zip'] = filename
        user_state['payer'] = payer
        
        # Extract the ZIP file and get list of files
        extracted_files = unzip_receipts(filename)
        user_state['extracted_files'] = extracted_files
        
        # Advance to next step
        user_state['current_step'] = 2  # Move to Extract step
        
        # Return the full page with updated state and success toast
        return render(request, 'start_page.html', {
            'current_step': user_state['current_step'],
            'show_success_toast': True,
            'extracted_files': user_state.get('extracted_files', []),
            'state': user_state.get_state(),
            'user': request.user
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

@require_POST
def restart(request):
    """Reset the state and return to step 1."""
    # Reset the global state
    user_state = request.user_state
    user_state['current_step'] = 0
    
    # Clear any stored data
    if 'upload_folder' in user_state:
        del user_state['upload_folder']
    if 'receipt_zip' in user_state:
        del user_state['receipt_zip']
    if 'extracted_files' in user_state:
        del user_state['extracted_files']
    if 'selected_file' in user_state:
        del user_state['selected_file']
    if 'payer' in user_state:
        del user_state['payer']
    if 'consumption' in user_state:
        del user_state['consumption']
    if 'sort_items' in user_state:
        del user_state['sort_items']
    if 'current_sort_index' in user_state:
        del user_state['current_sort_index']
    if 'api_costs_total' in user_state:
        del user_state['api_costs_total']
    
    # Render the full page with reset state
    return render(request, 'start_page.html', {
        'current_step': user_state['current_step'],
        'extracted_files': user_state.get('extracted_files', []),
        'state': user_state.get_state(),
        'user': request.user
    })

@require_GET
def get_step_template(request, step_number):
    """Return just the template content for HTMX updates."""
    if step_number == 1:
        return render(request, '1_read_the_docs.html')
    elif step_number == 2:
        return render(request, '2_upload_receipts.html')
    elif step_number == 3:
        return render(request, '3_extract_receipts.html')
    elif step_number == 4:
        return render(request, '4_sort.html', {
            'state': request.user_state.get_state()
        })
    elif step_number == 5:
        return render(request, '5_aggregate.html', {
            'state': request.user_state.get_state()
        })
    else:
        # Default to step 1 if invalid step number
        return render(request, '1_read_the_docs.html')

@require_POST
def select_file(request):
    """Handle file selection and return the updated main content area."""
    selected_file = request.POST.get('file')
    # Check if file exists in the new structure
    file_exists = any(item.get('file') == selected_file for item in request.user_state.get('extracted_files', []))
    if selected_file and file_exists:
        request.user_state['selected_file'] = selected_file
    
    # Return just the main content area with the selected file
    return render(request, '3_extract_receipts.html', {
        'selected_file': request.user_state.get('selected_file')
    })

@require_GET
def serve_image(request, filename):
    """Serve the selected image file."""
    if not request.user_state.get('receipt_zip'):
        return HttpResponse("No file uploaded", status=404)
    
    # Get the extraction directory
    zip_filename = request.user_state['receipt_zip']
    extract_dir_name = Path(zip_filename).stem
    image_path = Path(settings.BASE_DIR) / 'data' / '1_unzipped' / extract_dir_name / filename
    
    if not image_path.exists() or not image_path.is_file():
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
                                "Do not include any explanatory text, just the Python code for the list of dictionaries. Return the result as a Python list of dictionaries, without the markdown formatting of the code."
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

@require_POST
def extract_image_data(request):
    """Extract receipt data from the selected image using OpenAI API."""
    logger.debug("Starting image data extraction")
    logger.debug(f"Current STATE keys: {list(request.user_state.keys())}")
    
    selected_file = request.user_state.get('selected_file')
    logger.info(f"Extracting data from file: {selected_file}")
    
    if not selected_file:
        logger.error("No file selected for extraction")
        return HttpResponse('<div class="alert alert-error">No file selected</div>', status=400)
    
    # Check if we have the receipt_zip in state
    if not request.user_state.get('receipt_zip'):
        logger.error("No receipt ZIP file found in state")
        return HttpResponse('<div class="alert alert-error">No receipt ZIP file found in state</div>', status=400)
    
    # Get the image path
    zip_filename = request.user_state['receipt_zip']
    extract_dir_name = Path(zip_filename).stem
    image_path = Path(settings.BASE_DIR) / 'data' / '1_unzipped' / extract_dir_name / selected_file
    
    logger.debug(f"Zip filename: {zip_filename}")
    logger.debug(f"Extract dir name: {extract_dir_name}")
    logger.debug(f"Image path: {image_path}")
    logger.debug(f"Image path exists: {image_path.exists()}")
    
    if not image_path.exists():
        logger.error(f"Image file not found at: {image_path}")
        return HttpResponse(f'<div class="alert alert-error">Image file not found at: {image_path}</div>', status=404)
    
    # Get OpenAI API key from environment
    try:
        openai_api_key = os.environ['OPENAI_API_KEY']
        logger.debug(f"OpenAI API key configured: {bool(openai_api_key)}")
    except KeyError:
        logger.error("OpenAI API key not configured")
        return HttpResponse('<div class="alert alert-error">OpenAI API key not configured. Please set OPENAI_API_KEY in .config/.env file.</div>', status=500)
    
    try:
        logger.info(f"Starting AI extraction for {selected_file}")
        # Extract data from image
        extracted_data, cost = image_to_dataframe_dict(image_path, openai_api_key)
        logger.info(f"Extraction completed. Found {len(extracted_data)} items, cost: ${cost:.4f}")
        
        # Add cost to total API costs immediately after API call
        if 'api_costs_total' not in request.user_state:
            request.user_state['api_costs_total'] = 0
        request.user_state['api_costs_total'] += cost
        logger.info(f"Total API costs now: ${request.user_state['api_costs_total']:.4f}")
        
        # Store extracted data in state for later use
        request.user_state['extracted_data'] = extracted_data
        
        # Return the table template
        return render(request, 'extracted_data_table.html', {
            'extracted_data': extracted_data,
            'cost': cost,
            'selected_file': selected_file
        })
    except Exception as e:
        error_details = traceback.format_exc()
        logger.error(f"Extraction failed for {selected_file}: {str(e)}")
        logger.error(f"TRACEBACK: {error_details}")
        return HttpResponse(f'<div class="alert alert-error">Extraction failed: {str(e)}</div>', status=500)

@require_POST
def save_extraction(request):
    """Save the filtered extraction data to state."""
    try:
        file = request.POST.get('file')
        data_json = request.POST.get('data')
        
        if not file or not data_json:
            return JsonResponse({'error': 'Missing file or data'}, status=400)
        
        # Parse the JSON data
        filtered_data = json.loads(data_json)
        
        # Initialize extracted state if it doesn't exist
        if 'extracted' not in request.user_state:
            request.user_state['extracted'] = {}
        
        # Save the data for this specific file in the same format as confirm_extraction
        # request.user_state['extracted'][file_name] = [{"item": "", "price": ""}, ...]
        request.user_state['extracted'][file] = filtered_data
        
        logger.info(f"Saved {len(filtered_data)} items for {file}")
        
        return JsonResponse({
            'success': True,
            'message': f'Saved {len(filtered_data)} items for {file}'
        })
        
    except Exception as e:
        logger.error(f"Error saving extraction for {file}: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return JsonResponse({'error': f'Save failed: {str(e)}'}, status=500)

@require_POST
def confirm_extraction(request):
    """Confirm and finalize the extraction data, storing it in STATE."""
    try:
        extracted_data_json = request.POST.get('extracted_data')
        selected_file = request.POST.get('selected_file')
        
        logger.debug(f"Confirming extraction for file: {selected_file}")
        logger.debug(f"Data length: {len(extracted_data_json) if extracted_data_json else 0}")
        
        if not extracted_data_json or not selected_file:
            return JsonResponse({'error': 'Missing extracted_data or selected_file'}, status=400)
        
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
        
        # Initialize extracted state if it doesn't exist
        if 'extracted' not in request.user_state:
            request.user_state['extracted'] = {}
        
        # Store the confirmed data for this specific file in the exact format requested
        # request.user_state['extracted'][file_name] = [{"item": "", "price": ""}, ...]
        request.user_state['extracted'][selected_file] = extracted_data
        
        # Mark the file as extracted instead of removing it
        update_file_status(selected_file, 'extracted')
        
        logger.info(f"Confirmed extraction: {len(extracted_data)} items for {selected_file}")
        logger.debug(f"Data structure - Type: {type(extracted_data)}, Length: {len(extracted_data)}")
        if extracted_data:
            logger.debug(f"First item: {extracted_data[0]}")
        logger.debug(f"Current extracted files: {list(request.user_state.get('extracted', {}).keys())}")
        
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

@require_POST
def clear_selection(request):
    """Clear the selected file and return to initial extraction state."""
    try:
        # Clear the selected file from STATE
        if 'selected_file' in request.user_state:
            del request.user_state['selected_file']
        
        logger.debug("Cleared selected file from state")
        logger.debug(f"Remaining files to process: {len(get_files_by_status('tbd'))}")
        
        # Check if all files have been processed (no more TBD files)
        if not get_files_by_status('tbd') and request.user_state.get('extracted', {}):
            # All files have been processed, move to sorting step
            request.user_state['current_step'] = 3  # Move to Sort step
            logger.info("All files processed, advancing to Sort step")
        
        # Return the full page template with updated state
        return render(request, 'start_page.html', {
            'current_step': request.user_state['current_step'],
            'extracted_files': request.user_state.get('extracted_files', []),
            'state': request.user_state.get_state(),
            'user': request.user
        })
        
    except Exception as e:
        error_details = traceback.format_exc()
        logger.error(f"Clear selection failed: {str(e)}")
        logger.error(f"TRACEBACK: {error_details}")
        return HttpResponse(f'<div class="alert alert-error">Clear selection failed: {str(e)}</div>', status=500)

@require_POST
def start_sorting(request):
    """Initialize the sorting process by aggregating all extracted items."""
    try:
        logger.debug("Initializing sorting process")
        logger.debug(f"Extracted files available: {list(request.user_state.get('extracted', {}).keys())}")
        
        # Check if we have extracted data
        if not request.user_state.get('extracted'):
            return HttpResponse('<div class="alert alert-error">No extracted data found</div>', status=400)
        
        # Initialize consumption tracking if not exists
        if 'consumption' not in request.user_state:
            request.user_state['consumption'] = {
                'sebastian': [],
                'iva': [],
                'both': []
            }
        
        # Aggregate all items from all files into a single list
        all_items = []
        for filename, items in request.user_state.get('extracted', {}).items():
            for item in items:
                # Add filename for reference
                item_with_source = dict(item)
                item_with_source['source_file'] = filename
                all_items.append(item_with_source)
        
        request.user_state['sort_items'] = all_items
        request.user_state['current_sort_index'] = 0
        
        logger.info(f"Initialized sorting with {len(all_items)} total items")
        logger.debug(f"Items from {len(request.user_state['extracted'])} files")
        
        # Move to sorting step
        request.user_state['current_step'] = 3
        
        # Return the updated sort template
        return render(request, 'start_page.html', {
            'current_step': request.user_state['current_step'],
            'extracted_files': request.user_state.get('extracted_files', []),
            'state': request.user_state.get_state(),
            'user': request.user
        })
        
    except Exception as e:
        error_details = traceback.format_exc()
        logger.error(f"Sorting initialization failed: {str(e)}")
        logger.error(f"TRACEBACK: {error_details}")
        return HttpResponse(f'<div class="alert alert-error">Sorting initialization failed: {str(e)}</div>', status=500)

@require_POST
def assign_item(request):
    """Assign current item to a person (sebastian, iva, or both)."""
    try:
        assignee = request.POST.get('assignee')
        
        if assignee not in ['sebastian', 'iva', 'both']:
            return JsonResponse({'error': 'Invalid assignee'}, status=400)
        
        # Safety check: Initialize consumption if it doesn't exist
        if 'consumption' not in request.user_state:
            request.user_state['consumption'] = {
                'sebastian': [],
                'iva': [],
                'both': []
            }
            logger.warning("Consumption was not initialized, initializing now")
        
        current_index = request.user_state.get('current_sort_index', 0)
        sort_items = request.user_state.get('sort_items', [])
        
        if current_index >= len(sort_items):
            return JsonResponse({'error': 'No more items to sort'}, status=400)
        
        current_item = sort_items[current_index]
        
        # Add item to the appropriate list
        request.user_state['consumption'][assignee].append({
            'item': current_item['item'],
            'price': current_item['price'],
            'source_file': current_item.get('source_file', '')
        })
        
        # Move to next item
        request.user_state['current_sort_index'] += 1
        
        logger.info(f"Assigned '{current_item['item']}' (CHF {current_item['price']}) to {assignee}")
        logger.debug(f"Progress: {request.user_state['current_sort_index']}/{len(sort_items)}")
        
        # Check if we're done
        if request.user_state['current_sort_index'] >= len(sort_items):
            # All items sorted - move directly to aggregation
            request.user_state['current_step'] = 4
            calculate_aggregation(request.user_state)
            logger.info("All items sorted, advancing to Aggregation step")
            
            # Return just the aggregate template content with trigger to update sidebar
            response = render(request, '5_aggregate.html', {
                'state': request.user_state.get_state()
            })
            response['HX-Trigger'] = 'sortingComplete'
            return response
        
        # Return the updated sort template with next item
        return render(request, '4_sort.html', {
            'state': request.user_state.get_state()
        })
        
    except Exception as e:
        error_details = traceback.format_exc()
        logger.error(f"Item assignment failed: {str(e)}")
        logger.error(f"TRACEBACK: {error_details}")
        return JsonResponse({'error': f'Assignment failed: {str(e)}'}, status=500)

@require_GET
def get_current_sort_item(request):
    """Get the current item to be sorted and return HTML."""
    try:
        logger.debug("Getting current sort item")
        logger.debug(f"Current index: {request.user_state.get('current_sort_index', 0)}")
        
        # Check if we need to initialize sorting first
        if not request.user_state.get('sort_items') and request.user_state.get('extracted', {}):
            logger.debug("No sort_items found, initializing sorting...")
            # Initialize consumption tracking if not exists
            if 'consumption' not in request.user_state:
                request.user_state['consumption'] = {
                    'sebastian': [],
                    'iva': [],
                    'both': []
                }
            
            # Aggregate all items from all files into a single list
            all_items = []
            for filename, items in request.user_state.get('extracted', {}).items():
                for item in items:
                    # Add filename for reference
                    item_with_source = dict(item)
                    item_with_source['source_file'] = filename
                    all_items.append(item_with_source)
            
            request.user_state['sort_items'] = all_items
            request.user_state['current_sort_index'] = 0
            
            logger.info(f"Auto-initialized sorting with {len(all_items)} items")
        
        current_index = request.user_state.get('current_sort_index', 0)
        sort_items = request.user_state.get('sort_items', [])
        
        logger.debug(f"Current index: {current_index}, Total items: {len(sort_items)}")
        
        if len(sort_items) == 0:
            # No items to sort at all - move directly to aggregation
            request.user_state['current_step'] = 4
            # Ensure consumption is initialized as empty
            if 'consumption' not in request.user_state:
                request.user_state['consumption'] = {
                    'sebastian': [],
                    'iva': [],
                    'both': []
                }
            calculate_aggregation(request.user_state)
            logger.info("No items to sort, moving directly to aggregation")
            
            # Return just the aggregate template content with trigger to update sidebar
            response = render(request, '5_aggregate.html', {
                'state': request.user_state.get_state()
            })
            response['HX-Trigger'] = 'sortingComplete'
            return response
        
        if current_index >= len(sort_items):
            # All items sorted - move directly to aggregation
            request.user_state['current_step'] = 4
            calculate_aggregation(request.user_state)
            logger.info("All items sorted, showing aggregation results")
            
            # Return just the aggregate template content with trigger to update sidebar
            response = render(request, '5_aggregate.html', {
                'state': request.user_state.get_state()
            })
            response['HX-Trigger'] = 'sortingComplete'
            return response
        
        current_item = sort_items[current_index]
        total_items = len(sort_items)
        
        logger.debug(f"Displaying item: {current_item['item']} (CHF {current_item['price']})")
        
        # Calculate progress percentage
        progress_percentage = int((current_index / total_items) * 100) if total_items > 0 else 0
        
        # Return HTML for current item and progress update
        progress_text = f"Item {current_index + 1} of {total_items}"
        
        return HttpResponse(f"""
            <div class="space-y-4">
                <h3 class="text-xl font-bold text-base-content">{current_item['item']}</h3>
                <p class="text-2xl font-mono text-primary">CHF {current_item['price']}</p>
                <p class="text-sm text-base-content/60">From: {current_item.get('source_file', '')}</p>
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

def calculate_aggregation(user_state):
    """Calculate spending aggregation and transfer payment."""
    try:
        consumption = user_state.get('consumption', {})
        payer = user_state.get('payer', '')
        
        logger.debug(f"Calculating aggregation for payer: {payer}")
        logger.debug(f"Consumption data: {consumption}")
        logger.debug(f"Consumption categories: {[f'{k}={len(v)} items' for k, v in consumption.items()]}")
        
        # Calculate sums for each category
        sebastian_total = sum(float(item['price']) for item in consumption.get('sebastian', []))
        iva_total = sum(float(item['price']) for item in consumption.get('iva', []))
        both_total = sum(float(item['price']) for item in consumption.get('both', []))
        
        logger.debug(f"Raw totals - Sebastian: {sebastian_total}, Iva: {iva_total}, Both: {both_total}")
        
        # Calculate transfer payment based on payer
        if payer.lower() == 'iva':
            # Sebastian owes Iva: Sebastian's expenses + half of shared expenses
            transfer_amount = sebastian_total + (both_total / 2)
            transfer_direction = f"Sebastian → Iva"
        elif payer.lower() == 'sebastian':
            # Iva owes Sebastian: Iva's expenses + half of shared expenses
            transfer_amount = iva_total + (both_total / 2)
            transfer_direction = f"Iva → Sebastian"
        else:
            transfer_amount = 0
            transfer_direction = "No payer specified"
        
        aggregation_data = {
            'sebastian_total': sebastian_total,
            'iva_total': iva_total,
            'both_total': both_total,
            'transfer_amount': transfer_amount,
            'transfer_direction': transfer_direction,
            'payer': payer,
            'grand_total': sebastian_total + iva_total + both_total
        }
        
        # Store in STATE for access in template
        user_state['aggregation'] = aggregation_data
        
        logger.info(f"Aggregation calculated: Total CHF {aggregation_data['grand_total']:.2f}")
        logger.info(f"Transfer payment: {transfer_direction} CHF {transfer_amount:.2f}")
        logger.debug(f"Breakdown - Sebastian: CHF {sebastian_total:.2f}, Iva: CHF {iva_total:.2f}, Shared: CHF {both_total:.2f}")
        
        return aggregation_data
        
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

@require_POST
def start_extraction(request):
    """Initialize the extraction process and set the first file as current."""
    try:
        if not request.user_state.get('extracted_files'):
            return HttpResponse('<div class="alert alert-error">No files to extract</div>', status=400)
        
        # Get the first TBD file
        next_file = get_next_tbd_file(request.user_state)
        if not next_file:
            return HttpResponse('<div class="alert alert-error">No files need processing</div>', status=400)
        
        request.user_state['current_file'] = next_file['file']
        
        # Initialize extraction tracking
        if 'extracted' not in request.user_state:
            request.user_state['extracted'] = {}
        
        # Initialize progress tracking
        total_files = get_total_files(request.user_state)
        files_processed = get_processed_files_count(request.user_state)
        request.user_state['files_processed'] = files_processed
        request.user_state['progress_percentage'] = int((files_processed / total_files) * 100) if total_files > 0 else 0
        
        logger.info(f"Started extraction process with {total_files} total files")
        logger.info(f"First TBD file: {request.user_state['current_file']}")
        
        # Return the full page with updated state
        return render(request, 'start_page.html', {
            'current_step': request.user_state['current_step'],
            'extracted_files': request.user_state.get('extracted_files', []),
            'state': request.user_state.get_state(),
            'user': request.user
        })
        
    except Exception as e:
        logger.error(f"Failed to start extraction: {str(e)}")
        return HttpResponse(f'<div class="alert alert-error">Failed to start extraction: {str(e)}</div>', status=500)

@require_POST
def extract_current_image(request):
    """Extract receipt data from the current image in the sequence."""
    try:
        current_file = request.user_state.get('current_file')
        if not current_file:
            logger.error("No current file set for extraction")
            return HttpResponse('<div class="alert alert-error">No current file set</div>', status=400)
        
        logger.info(f"Extracting data from current file: {current_file}")
        
        # Check if we have the receipt_zip in state
        if not request.user_state.get('receipt_zip'):
            logger.error("No receipt ZIP file found in state")
            return HttpResponse('<div class="alert alert-error">No receipt ZIP file found in state</div>', status=400)
        
        # Get the image path
        zip_filename = request.user_state['receipt_zip']
        extract_dir_name = Path(zip_filename).stem
        image_path = Path(settings.BASE_DIR) / 'data' / '1_unzipped' / extract_dir_name / current_file
        
        if not image_path.exists():
            logger.error(f"Image file not found at: {image_path}")
            return HttpResponse(f'<div class="alert alert-error">Image file not found</div>', status=404)
        
        # Get OpenAI API key from environment
        try:
            openai_api_key = os.environ['OPENAI_API_KEY']
        except KeyError:
            logger.error("OpenAI API key not configured")
            return HttpResponse('<div class="alert alert-error">OpenAI API key not configured</div>', status=500)
        
        # Extract data from image
        extracted_data, cost = image_to_dataframe_dict(image_path, openai_api_key)
        logger.info(f"Extraction completed. Found {len(extracted_data)} items, cost: ${cost:.4f}")
        
        # Add cost to total API costs
        if 'api_costs_total' not in request.user_state:
            request.user_state['api_costs_total'] = 0
        request.user_state['api_costs_total'] += cost
        logger.info(f"Total API costs now: ${request.user_state['api_costs_total']:.4f}")
        
        # Store extracted data in state for later use
        request.user_state['extracted_data'] = extracted_data
        
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

@require_POST
def skip_current_file(request):
    """Skip the current file and move to the next one."""
    try:
        current_file = request.user_state.get('current_file')
        if current_file:
            logger.info(f"Skipping file: {current_file}")
            # Mark as skipped instead of removing
            update_file_status(current_file, 'skipped')
        
        return next_file(request)
        
    except Exception as e:
        logger.error(f"Failed to skip current file: {str(e)}")
        return HttpResponse(f'<div class="alert alert-error">Failed to skip file: {str(e)}</div>', status=500)

@require_POST  
def next_file(request):
    """Move to the next file in the extraction sequence."""
    try:
        # Get current progress
        total_files = get_total_files(request.user_state)
        files_processed = get_processed_files_count(request.user_state)
        progress_percentage = int((files_processed / total_files) * 100) if total_files > 0 else 0
        
        request.user_state['files_processed'] = files_processed
        request.user_state['progress_percentage'] = progress_percentage
        
        # Get next TBD file
        next_file = get_next_tbd_file(request.user_state)
        
        if next_file:
            # Set next TBD file as current
            request.user_state['current_file'] = next_file['file']
            logger.info(f"Moving to next TBD file: {request.user_state['current_file']}")
        else:
            # All files processed
            request.user_state['current_file'] = None
            logger.info("All files processed, checking what to do next")
            
            # Prepare sort items from all extracted data
            sort_items = []
            if request.user_state.get('extracted', {}):
                for filename, data in request.user_state['extracted'].items():
                    for item in data:
                        sort_items.append({
                            'item': item['item'],
                            'price': float(item['price']),
                            'source_file': filename
                        })
                logger.info(f"Found {len(sort_items)} items from extracted data")
            
            if sort_items:
                # We have items to sort - go to Sort step
                request.user_state['current_step'] = 3  # Move to Sort step
                request.user_state['sort_items'] = sort_items
                request.user_state['current_sort_index'] = 0
                logger.info(f"Moving to Sort step with {len(sort_items)} items")
            else:
                # No items to sort - skip directly to aggregation with empty consumption
                request.user_state['current_step'] = 4  # Move to Aggregate step
                request.user_state['sort_items'] = []
                request.user_state['current_sort_index'] = 0
                # Initialize empty consumption
                request.user_state['consumption'] = {
                    'sebastian': [],
                    'iva': [],
                    'both': []
                }
                calculate_aggregation(request.user_state)
                logger.info("No items to sort, skipping directly to Aggregation step")
        
        # Return the full page with updated state
        return render(request, 'start_page.html', {
            'current_step': request.user_state['current_step'],
            'extracted_files': request.user_state.get('extracted_files', []),
            'state': request.user_state.get_state(),
            'user': request.user
        })
        
    except Exception as e:
        logger.error(f"Failed to advance to next file: {str(e)}")
        return HttpResponse(f'<div class="alert alert-error">Failed to advance: {str(e)}</div>', status=500)

def get_files_by_status(status):
    """Get all files with a specific status from extracted_files."""
    return [item for item in request.user_state.get('extracted_files', []) if item.get('status') == status]

def get_next_tbd_file(user_state):
    """Get the next file with 'tbd' status."""
    tbd_files = get_files_by_status('tbd')
    return tbd_files[0] if tbd_files else None

def update_file_status(filename, new_status):
    """Update the status of a specific file in extracted_files."""
    for item in request.user_state.get('extracted_files', []):
        if item.get('file') == filename:
            item['status'] = new_status
            logger.info(f"Updated {filename} status to {new_status}")
            return True
    logger.warning(f"File {filename} not found in extracted_files")
    return False

def get_total_files(user_state):
    """Get total number of files."""
    return len(user_state.get('extracted_files', []))

def get_processed_files_count(user_state):
    """Get count of files that have been processed (extracted or skipped)."""
    return len([item for item in user_state.get('extracted_files', []) if item.get('status') != 'tbd'])
