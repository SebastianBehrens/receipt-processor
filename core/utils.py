from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, get_object_or_404
from django.urls import reverse
from django.conf import settings
from functools import wraps
from pathlib import Path

from .models import UserState, ProcessingSession
import logging

logger = logging.getLogger(__name__)

ACTIVE_SESSION_KEY = 'active_processing_session_id'

STEP_LABELS = UserState.STEP_LABELS


def get_session_storage_path(session_id):
    return f'sessions/{session_id}/'


def create_processing_session(user):
    session = ProcessingSession.objects.create(
        user=user,
        state=ProcessingSession.default_state(),
    )
    session.storage_path = get_session_storage_path(session.id)
    session.update_title()
    session.save(update_fields=['storage_path', 'title'])

    session.get_storage_dir().mkdir(parents=True, exist_ok=True)
    session.get_upload_dir().mkdir(parents=True, exist_ok=True)
    session.get_unzipped_dir().mkdir(parents=True, exist_ok=True)
    return session


def get_active_session_id(request):
    return request.session.get(ACTIVE_SESSION_KEY)


def set_active_session(request, session):
    request.session[ACTIVE_SESSION_KEY] = session.id


def clear_active_session(request):
    request.session.pop(ACTIVE_SESSION_KEY, None)


def get_active_session(request):
    session_id = get_active_session_id(request)
    if not session_id or not request.user.is_authenticated:
        return None
    try:
        return ProcessingSession.objects.get(id=session_id, user=request.user)
    except ProcessingSession.DoesNotExist:
        clear_active_session(request)
        return None


def get_user_state(request):
    session = get_active_session(request)
    if not session:
        return None
    return UserState(session)


def get_session_unzipped_dir(user_state):
    return user_state.session.get_unzipped_dir()


def get_image_path(user_state, filename):
    extract_dir = get_session_unzipped_dir(user_state).resolve()
    image_path = (extract_dir / filename).resolve()
    image_path.relative_to(extract_dir)
    return image_path


def normalize_price(price):
    """Normalize prices so float/string representations match during lookup."""
    try:
        return f"{float(price):.2f}"
    except (TypeError, ValueError):
        return str(price or '').strip()


def item_lookup_key(source_file, item_name, price):
    return (
        str(source_file or '').strip(),
        str(item_name or '').strip(),
        normalize_price(price),
    )


ASSIGNEE_BADGES = {
    'sebastian': ('Sebastian', 'badge-primary'),
    'iva': ('Iva', 'badge-secondary'),
    'both': ('50-50', 'badge-accent'),
}


def format_assignee(bucket):
    return ASSIGNEE_BADGES.get(bucket, ('Unknown', 'badge-ghost'))


def lookup_assignee(consumption, source_file, item_name, price):
    target_key = item_lookup_key(source_file, item_name, price)
    for bucket, items in (consumption or {}).items():
        for item in items or []:
            if item_lookup_key(
                item.get('source_file', ''),
                item.get('item', ''),
                item.get('price', ''),
            ) == target_key:
                return bucket
    return 'unknown'


def build_receipt_details(state):
    consumption = state.get('consumption', {})
    receipts = []

    for file_entry in state.get('extracted_files', []):
        if file_entry.get('status') != 'extracted':
            continue
        filename = file_entry.get('file', '')
        items = state.get('extracted', {}).get(filename, [])
        line_items = []
        for item in items:
            assignee = lookup_assignee(
                consumption,
                filename,
                item.get('item', ''),
                item.get('price', ''),
            )
            label, badge = format_assignee(assignee)
            line_items.append({
                'item': item.get('item', ''),
                'price': item.get('price', ''),
                'assignee': assignee,
                'assignee_label': label,
                'assignee_badge': badge,
            })
        receipts.append({
            'filename': filename,
            'items': line_items,
        })
    return receipts


def require_auth(view_func):
    """Decorator to require authentication."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            logger.warning("Unauthenticated access attempt to %s", view_func.__name__)
            return redirect(reverse('core:login'))
        return view_func(request, *args, **kwargs)
    return wrapper


def require_active_session(view_func):
    """Decorator to require an active processing session."""
    @wraps(view_func)
    @require_auth
    def wrapper(request, *args, **kwargs):
        if not request.user_state:
            return redirect(reverse('core:session_hub'))
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
            logger.info(
                "Updated %s status to %s for session %s",
                filename,
                new_status,
                user_state.session.pk,
            )
            return True
    logger.warning(
        "File %s not found in extracted_files for session %s",
        filename,
        user_state.session.pk,
    )
    return False


def get_total_files(user_state):
    """Get total number of files for the user."""
    return len(user_state.get('extracted_files', []))


def get_processed_files_count(user_state):
    """Get count of files that have been processed (extracted or skipped)."""
    return len([
        item for item in user_state.get('extracted_files', [])
        if item.get('status') != 'tbd'
    ])


def ensure_consumption_initialized(user_state):
    """Ensure consumption is properly initialized."""
    state = user_state.get_state()
    if 'consumption' not in state:
        state['consumption'] = {
            'sebastian': [],
            'iva': [],
            'both': [],
        }
        user_state.save_state(state)
        logger.info("Initialized consumption for session %s", user_state.session.pk)
    return state['consumption']


def store_extracted_data(user_state, filename, data):
    """Persist extracted line items for a receipt image."""
    state = user_state.get_state()
    state.setdefault('extracted', {})[filename] = data
    user_state.save_state(state)


def prepare_sort_display(user_state):
    """Ensure sort state exists and return display context for the current item."""
    if not user_state.get('sort_items') and user_state.get('extracted', {}):
        ensure_consumption_initialized(user_state)
        all_items = []
        for filename, items in user_state.get('extracted', {}).items():
            for item in items:
                item_with_source = dict(item)
                item_with_source['source_file'] = filename
                all_items.append(item_with_source)
        user_state['sort_items'] = all_items
        user_state['current_sort_index'] = 0

    current_index = user_state.get('current_sort_index', 0)
    sort_items = user_state.get('sort_items', [])
    total_items = len(sort_items)

    if total_items == 0 or current_index >= total_items:
        return None

    progress_percentage = int((current_index / total_items) * 100) if total_items > 0 else 0
    return {
        'current_item': sort_items[current_index],
        'current_index': current_index,
        'current_number': current_index + 1,
        'total_items': total_items,
        'progress_percentage': progress_percentage,
    }


def get_user_session_or_404(request, session_id):
    return get_object_or_404(
        ProcessingSession,
        id=session_id,
        user=request.user,
    )
