from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect
from django.urls import reverse
from functools import wraps
from .models import UserState
import logging

logger = logging.getLogger(__name__)

def get_user_state(request):
    """Get or create user state for the current request."""
    if not request.user.is_authenticated:
        return None
    return UserState(request.user.id)

def require_auth(view_func):
    """Decorator to require authentication and provide user state."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            logger.warning(f"Unauthenticated access attempt to {view_func.__name__}")
            return redirect(reverse('login'))
        
        # Add user_state to request for easy access
        request.user_state = get_user_state(request)
        return view_func(request, *args, **kwargs)
    return wrapper

def get_files_by_status(user_state, status):
    """Get all files with a specific status from user's extracted_files."""
    return [item for item in user_state.get('extracted_files', []) if item.get('status') == status]

def get_next_tbd_file(user_state):
    """Get the next file with 'tbd' status for the user."""
    tbd_files = get_files_by_status(user_state, 'tbd')
    return tbd_files[0] if tbd_files else None

def update_file_status(user_state, filename, new_status):
    """Update the status of a specific file in user's extracted_files."""
    state = user_state.get_state()
    for item in state.get('extracted_files', []):
        if item.get('file') == filename:
            item['status'] = new_status
            user_state.save_state(state)
            logger.info(f"Updated {filename} status to {new_status} for user {user_state.user_id}")
            return True
    logger.warning(f"File {filename} not found in extracted_files for user {user_state.user_id}")
    return False

def get_total_files(user_state):
    """Get total number of files for the user."""
    return len(user_state.get('extracted_files', []))

def get_processed_files_count(user_state):
    """Get count of files that have been processed (extracted or skipped) for the user."""
    return len([item for item in user_state.get('extracted_files', []) if item.get('status') != 'tbd'])

def ensure_consumption_initialized(user_state):
    """Ensure consumption is properly initialized for the user."""
    state = user_state.get_state()
    if 'consumption' not in state:
        state['consumption'] = {
            'sebastian': [],
            'iva': [],
            'both': []
        }
        user_state.save_state(state)
        logger.info(f"Initialized consumption for user {user_state.user_id}")
    return state['consumption'] 