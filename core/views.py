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
from django.shortcuts import render
from django.http import HttpResponse, FileResponse, JsonResponse
from django.conf import settings
from django.views.decorators.http import require_GET, require_POST
from django.utils.text import get_valid_filename
from dotenv import load_dotenv

# Load environment variables from config/.env
load_dotenv(Path(settings.BASE_DIR) / 'config' / '.env')

# Set up logging
logger = logging.getLogger(__name__)

# Global state to track current step
STATE = {
    'current_step': 0,  # 0 = Read docs, 1 = Upload, 2 = Extract, 3 = Sort, 4 = Aggregate
    'receipt_zip': None,
    'extracted_files': [],
    'selected_file': None,
    'payer': None,  # Store the selected payer
    'consumption': {
        'sebastian': [],
        'iva': [],
        'both': []
    },
    'sort_items': [],  # All items to be sorted
    'current_sort_index': 0,  # Current item being sorted
    'api_costs_total': 0,  # Track total API costs
    'current_extraction_index': 0,
    'files_processed': 0,
    'progress_percentage': 0
}

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
                    extracted_files.append(str(relative_path))
    
    except Exception as e:
        logger.error(f"Error extracting ZIP file: {e}")
        return []
    
    return extracted_files

@require_GET
def start_page(request):
    """Render the start page for the HTMX Receipt Processor."""
    return render(request, 'start_page.html', {
        'current_step': STATE['current_step'],
        'extracted_files': STATE.get('extracted_files', []),
        'state': STATE
    })

@require_POST
def step_view(request, step_number):
    """Main view to handle different steps of the receipt processing workflow."""
    logger.info(f"Step view requested: {step_number}")
    
    # Update current step (convert to 0-based index for internal use)
    STATE['current_step'] = step_number - 1
    
    # If moving to aggregation step (step 5 = index 4), calculate aggregation
    if step_number == 5:
        calculate_aggregation()
    
    # Calculate progress information for extraction step
    if step_number == 3 and STATE.get('extracted_files'):  # Step 3 is extraction (0-based index 2)
        current_index = STATE.get('current_extraction_index', 0)
        total_files = len(STATE['extracted_files'])
        files_processed = current_index
        progress_percentage = int((files_processed / total_files) * 100) if total_files > 0 else 0
        
        STATE['files_processed'] = files_processed
        STATE['progress_percentage'] = progress_percentage
        
        logger.debug(f"Extraction progress: {files_processed}/{total_files} ({progress_percentage}%)")
    
    # Create context with state information
    context = {
        'current_step': STATE['current_step'],
        'extracted_files': STATE.get('extracted_files', []),
        'state': STATE
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
        STATE['receipt_zip'] = filename
        STATE['payer'] = payer
        
        # Extract the ZIP file and get list of files
        extracted_files = unzip_receipts(filename)
        STATE['extracted_files'] = extracted_files
        
        # Advance to next step
        STATE['current_step'] = 2  # Move to Extract step
        
        # Return the full page with updated state and success toast
        return render(request, 'start_page.html', {
            'current_step': STATE['current_step'],
            'show_success_toast': True,
            'extracted_files': STATE.get('extracted_files', []),
            'state': STATE
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
    STATE['current_step'] = 0
    
    # Clear any stored data
    if 'upload_folder' in STATE:
        del STATE['upload_folder']
    if 'receipt_zip' in STATE:
        del STATE['receipt_zip']
    if 'extracted_files' in STATE:
        del STATE['extracted_files']
    if 'selected_file' in STATE:
        del STATE['selected_file']
    if 'payer' in STATE:
        del STATE['payer']
    if 'consumption' in STATE:
        del STATE['consumption']
    if 'sort_items' in STATE:
        del STATE['sort_items']
    if 'current_sort_index' in STATE:
        del STATE['current_sort_index']
    if 'api_costs_total' in STATE:
        del STATE['api_costs_total']
    
    # Render the full page with reset state
    return render(request, 'start_page.html', {
        'current_step': STATE['current_step'],
        'extracted_files': STATE.get('extracted_files', []),
        'state': STATE
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
            'state': STATE
        })
    elif step_number == 5:
        return render(request, '5_aggregate.html', {
            'state': STATE
        })
    else:
        # Default to step 1 if invalid step number
        return render(request, '1_read_the_docs.html')

@require_POST
def select_file(request):
    """Handle file selection and return the updated main content area."""
    selected_file = request.POST.get('file')
    if selected_file and selected_file in STATE.get('extracted_files', []):
        STATE['selected_file'] = selected_file
    
    # Return just the main content area with the selected file
    return render(request, '3_extract_receipts.html', {
        'selected_file': STATE.get('selected_file')
    })

@require_GET
def serve_image(request, filename):
    """Serve the selected image file."""
    if not STATE.get('receipt_zip'):
        return HttpResponse("No file uploaded", status=404)
    
    # Get the extraction directory
    zip_filename = STATE['receipt_zip']
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
    logger.debug(f"Current STATE keys: {list(STATE.keys())}")
    
    selected_file = STATE.get('selected_file')
    logger.info(f"Extracting data from file: {selected_file}")
    
    if not selected_file:
        logger.error("No file selected for extraction")
        return HttpResponse('<div class="alert alert-error">No file selected</div>', status=400)
    
    # Check if we have the receipt_zip in state
    if not STATE.get('receipt_zip'):
        logger.error("No receipt ZIP file found in state")
        return HttpResponse('<div class="alert alert-error">No receipt ZIP file found in state</div>', status=400)
    
    # Get the image path
    zip_filename = STATE['receipt_zip']
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
        if 'api_costs_total' not in STATE:
            STATE['api_costs_total'] = 0
        STATE['api_costs_total'] += cost
        logger.info(f"Total API costs now: ${STATE['api_costs_total']:.4f}")
        
        # Store extracted data in state for later use
        STATE['extracted_data'] = extracted_data
        
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
        if 'extracted' not in STATE:
            STATE['extracted'] = {}
        
        # Save the data for this specific file in the same format as confirm_extraction
        # STATE['extracted'][file_name] = [{"item": "", "price": ""}, ...]
        STATE['extracted'][file] = filtered_data
        
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
        if 'extracted' not in STATE:
            STATE['extracted'] = {}
        
        # Store the confirmed data for this specific file in the exact format requested
        # STATE['extracted'][file_name] = [{"item": "", "price": ""}, ...]
        STATE['extracted'][selected_file] = extracted_data
        
        # Remove the processed file from extracted_files list so it doesn't appear in sidebar
        if 'extracted_files' in STATE and selected_file in STATE['extracted_files']:
            STATE['extracted_files'].remove(selected_file)
            logger.info(f"Removed {selected_file} from processing queue")
        
        logger.info(f"Confirmed extraction: {len(extracted_data)} items for {selected_file}")
        logger.debug(f"Data structure - Type: {type(extracted_data)}, Length: {len(extracted_data)}")
        if extracted_data:
            logger.debug(f"First item: {extracted_data[0]}")
        logger.debug(f"Current extracted files: {list(STATE.get('extracted', {}).keys())}")
        
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
        if 'selected_file' in STATE:
            del STATE['selected_file']
        
        logger.debug("Cleared selected file from state")
        logger.debug(f"Remaining files to process: {len(STATE.get('extracted_files', []))}")
        
        # Check if all files have been processed (extracted_files should be empty)
        if not STATE.get('extracted_files') and STATE.get('extracted', {}):
            # All files have been processed, move to sorting step
            STATE['current_step'] = 3  # Move to Sort step
            logger.info("All files processed, advancing to Sort step")
        
        # Return the full page template with updated state
        return render(request, 'start_page.html', {
            'current_step': STATE['current_step'],
            'extracted_files': STATE.get('extracted_files', []),
            'state': STATE
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
        logger.debug(f"Extracted files available: {list(STATE.get('extracted', {}).keys())}")
        
        # Check if we have extracted data
        if not STATE.get('extracted'):
            return HttpResponse('<div class="alert alert-error">No extracted data found</div>', status=400)
        
        # Initialize consumption tracking if not exists
        if 'consumption' not in STATE:
            STATE['consumption'] = {
                'sebastian': [],
                'iva': [],
                'both': []
            }
        
        # Aggregate all items from all files into a single list
        all_items = []
        for filename, items in STATE['extracted'].items():
            for item in items:
                # Add filename for reference
                item_with_source = dict(item)
                item_with_source['source_file'] = filename
                all_items.append(item_with_source)
        
        STATE['sort_items'] = all_items
        STATE['current_sort_index'] = 0
        
        logger.info(f"Initialized sorting with {len(all_items)} total items")
        logger.debug(f"Items from {len(STATE['extracted'])} files")
        
        # Move to sorting step
        STATE['current_step'] = 3
        
        # Return the updated sort template
        return render(request, 'start_page.html', {
            'current_step': STATE['current_step'],
            'extracted_files': STATE.get('extracted_files', []),
            'state': STATE
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
        
        current_index = STATE.get('current_sort_index', 0)
        sort_items = STATE.get('sort_items', [])
        
        if current_index >= len(sort_items):
            return JsonResponse({'error': 'No more items to sort'}, status=400)
        
        current_item = sort_items[current_index]
        
        # Add item to the appropriate list
        STATE['consumption'][assignee].append({
            'item': current_item['item'],
            'price': current_item['price'],
            'source_file': current_item.get('source_file', '')
        })
        
        # Move to next item
        STATE['current_sort_index'] += 1
        
        logger.info(f"Assigned '{current_item['item']}' (CHF {current_item['price']}) to {assignee}")
        logger.debug(f"Progress: {STATE['current_sort_index']}/{len(sort_items)}")
        
        # Check if we're done
        if STATE['current_sort_index'] >= len(sort_items):
            # All items sorted - move directly to aggregation
            STATE['current_step'] = 4
            calculate_aggregation()
            logger.info("All items sorted, advancing to Aggregation step")
            
            # Return just the aggregate template content with trigger to update sidebar
            response = render(request, '5_aggregate.html', {
                'state': STATE
            })
            response['HX-Trigger'] = 'sortingComplete'
            return response
        
        # Return the updated sort template with next item
        return render(request, '4_sort.html', {
            'state': STATE
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
        logger.debug(f"Current index: {STATE.get('current_sort_index', 0)}")
        
        # Check if we need to initialize sorting first
        if not STATE.get('sort_items') and STATE.get('extracted', {}):
            logger.debug("No sort_items found, initializing sorting...")
            # Initialize consumption tracking if not exists
            if 'consumption' not in STATE:
                STATE['consumption'] = {
                    'sebastian': [],
                    'iva': [],
                    'both': []
                }
            
            # Aggregate all items from all files into a single list
            all_items = []
            for filename, items in STATE['extracted'].items():
                for item in items:
                    # Add filename for reference
                    item_with_source = dict(item)
                    item_with_source['source_file'] = filename
                    all_items.append(item_with_source)
            
            STATE['sort_items'] = all_items
            STATE['current_sort_index'] = 0
            
            logger.info(f"Auto-initialized sorting with {len(all_items)} items")
        
        current_index = STATE.get('current_sort_index', 0)
        sort_items = STATE.get('sort_items', [])
        
        logger.debug(f"Current index: {current_index}, Total items: {len(sort_items)}")
        
        if current_index >= len(sort_items):
            # All items sorted - move directly to aggregation
            STATE['current_step'] = 4
            calculate_aggregation()
            logger.info("All items sorted, showing aggregation results")
            
            # Return just the aggregate template content with trigger to update sidebar
            response = render(request, '5_aggregate.html', {
                'state': STATE
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

def calculate_aggregation():
    """Calculate spending aggregation and transfer payment."""
    try:
        consumption = STATE.get('consumption', {})
        payer = STATE.get('payer', '')
        
        logger.debug(f"Calculating aggregation for payer: {payer}")
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
        STATE['aggregation'] = aggregation_data
        
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
        if not STATE.get('extracted_files'):
            return HttpResponse('<div class="alert alert-error">No files to extract</div>', status=400)
        
        # Set the first file as current
        STATE['current_extraction_index'] = 0
        STATE['current_file'] = STATE['extracted_files'][0]
        
        # Initialize extraction tracking
        if 'extracted' not in STATE:
            STATE['extracted'] = {}
        
        # Initialize progress tracking
        total_files = len(STATE['extracted_files'])
        STATE['files_processed'] = 0
        STATE['progress_percentage'] = 0
        
        logger.info(f"Started extraction process with {total_files} files")
        logger.info(f"First file: {STATE['current_file']}")
        
        # Return the full page with updated state
        return render(request, 'start_page.html', {
            'current_step': STATE['current_step'],
            'extracted_files': STATE.get('extracted_files', []),
            'state': STATE
        })
        
    except Exception as e:
        logger.error(f"Failed to start extraction: {str(e)}")
        return HttpResponse(f'<div class="alert alert-error">Failed to start extraction: {str(e)}</div>', status=500)

@require_POST
def extract_current_image(request):
    """Extract receipt data from the current image in the sequence."""
    try:
        current_file = STATE.get('current_file')
        if not current_file:
            logger.error("No current file set for extraction")
            return HttpResponse('<div class="alert alert-error">No current file set</div>', status=400)
        
        logger.info(f"Extracting data from current file: {current_file}")
        
        # Check if we have the receipt_zip in state
        if not STATE.get('receipt_zip'):
            logger.error("No receipt ZIP file found in state")
            return HttpResponse('<div class="alert alert-error">No receipt ZIP file found in state</div>', status=400)
        
        # Get the image path
        zip_filename = STATE['receipt_zip']
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
        if 'api_costs_total' not in STATE:
            STATE['api_costs_total'] = 0
        STATE['api_costs_total'] += cost
        logger.info(f"Total API costs now: ${STATE['api_costs_total']:.4f}")
        
        # Store extracted data in state for later use
        STATE['extracted_data'] = extracted_data
        
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
        current_file = STATE.get('current_file')
        if current_file:
            logger.info(f"Skipping file: {current_file}")
            # Remove from extracted_files list
            if current_file in STATE.get('extracted_files', []):
                STATE['extracted_files'].remove(current_file)
        
        return next_file(request)
        
    except Exception as e:
        logger.error(f"Failed to skip current file: {str(e)}")
        return HttpResponse(f'<div class="alert alert-error">Failed to skip file: {str(e)}</div>', status=500)

@require_POST  
def next_file(request):
    """Move to the next file in the extraction sequence."""
    try:
        # Get current progress
        current_index = STATE.get('current_extraction_index', 0)
        extracted_files = STATE.get('extracted_files', [])
        
        # Move to next file
        current_index += 1
        STATE['current_extraction_index'] = current_index
        
        # Calculate progress
        total_files = len(extracted_files)
        files_processed = current_index
        progress_percentage = int((files_processed / total_files) * 100) if total_files > 0 else 0
        
        STATE['files_processed'] = files_processed
        STATE['progress_percentage'] = progress_percentage
        
        if current_index < len(extracted_files):
            # Set next file as current
            STATE['current_file'] = extracted_files[current_index]
            logger.info(f"Moving to next file: {STATE['current_file']} ({current_index + 1}/{len(extracted_files)})")
        else:
            # All files processed
            STATE['current_file'] = None
            STATE['current_step'] = 3  # Move to Sort step
            logger.info("All files processed, advancing to Sort step")
            
            # Prepare sort items from all extracted data
            if STATE.get('extracted', {}):
                sort_items = []
                for filename, data in STATE['extracted'].items():
                    for item in data:
                        sort_items.append({
                            'item': item['item'],
                            'price': float(item['price']),
                            'filename': filename
                        })
                STATE['sort_items'] = sort_items
                STATE['current_sort_index'] = 0
                logger.info(f"Prepared {len(sort_items)} items for sorting")
        
        # Return the full page with updated state
        return render(request, 'start_page.html', {
            'current_step': STATE['current_step'],
            'extracted_files': STATE.get('extracted_files', []),
            'state': STATE
        })
        
    except Exception as e:
        logger.error(f"Failed to advance to next file: {str(e)}")
        return HttpResponse(f'<div class="alert alert-error">Failed to advance: {str(e)}</div>', status=500)
