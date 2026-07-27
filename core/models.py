from django.contrib.auth.models import AbstractUser
from django.db import models
from django.conf import settings
from django.utils import timezone
from pathlib import Path
import copy
import logging

logger = logging.getLogger(__name__)


class User(AbstractUser):
    """Custom user model for receipt processor users."""
    display_name = models.CharField(max_length=100, blank=True)

    def __str__(self):
        return self.username


class ProcessingSession(models.Model):
    STATUS_IN_PROGRESS = 'in_progress'
    STATUS_COMPLETED = 'completed'
    STATUS_ABANDONED = 'abandoned'

    STATUS_CHOICES = [
        (STATUS_IN_PROGRESS, 'In Progress'),
        (STATUS_COMPLETED, 'Completed'),
        (STATUS_ABANDONED, 'Abandoned'),
    ]

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='processing_sessions',
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_IN_PROGRESS,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    title = models.CharField(max_length=255, blank=True)
    payer = models.CharField(max_length=50, blank=True)
    receipt_zip = models.CharField(max_length=255, blank=True)
    state = models.JSONField(default=dict)
    storage_path = models.CharField(max_length=512, blank=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return self.title or f"Session {self.pk}"

    @classmethod
    def default_state(cls):
        return {
            'current_step': 0,
            'receipt_zip': None,
            'extracted_files': [],
            'selected_file': None,
            'payer': None,
            'consumption': {
                'sebastian': [],
                'iva': [],
                'both': [],
            },
            'sort_items': [],
            'current_sort_index': 0,
            'api_costs_total': 0,
            'current_extraction_index': 0,
            'files_processed': 0,
            'progress_percentage': 0,
            'current_file': None,
            'extracted': {},
            'aggregation': {},
        }

    def get_storage_dir(self):
        return Path(settings.BASE_DIR) / 'data' / self.storage_path

    def get_upload_dir(self):
        return self.get_storage_dir() / 'uploaded'

    def get_unzipped_dir(self):
        return self.get_storage_dir() / 'unzipped'

    def update_title(self):
        if self.receipt_zip:
            date_str = self.created_at.strftime('%d %b %Y')
            self.title = f"{date_str} — {self.receipt_zip}"
        elif not self.title:
            self.title = self.created_at.strftime('%d %b %Y — New session')

    def mark_completed(self):
        if self.status != self.STATUS_COMPLETED:
            self.status = self.STATUS_COMPLETED
            self.completed_at = timezone.now()
            self.save(update_fields=['status', 'completed_at', 'updated_at'])

    def get_current_step_label(self):
        step = (self.state or {}).get('current_step', 0)
        if 0 <= step < len(UserState.STEP_LABELS):
            return UserState.STEP_LABELS[step]
        return 'Unknown'

    def get_grand_total(self):
        aggregation = (self.state or {}).get('aggregation', {})
        return aggregation.get('grand_total')


class UserState:
    """Manages workflow state backed by a ProcessingSession record."""

    STEP_LABELS = ['Read Docs', 'Upload', 'Extract', 'Sort', 'Aggregate']

    def __init__(self, session: ProcessingSession):
        self.session = session
        self.user_id = session.user_id
        self.default_state = ProcessingSession.default_state()

    def get_state(self):
        if not self.session.state:
            state = copy.deepcopy(self.default_state)
            self.save_state(state)
            return state
        return self.session.state

    def save_state(self, state):
        self.session.state = state
        self.session.payer = state.get('payer') or ''
        receipt_zip = state.get('receipt_zip')
        self.session.receipt_zip = receipt_zip or ''
        self.session.update_title()
        self.session.save(
            update_fields=['state', 'payer', 'receipt_zip', 'title', 'updated_at']
        )
        logger.debug("Saved state for session %s", self.session.pk)

    def update_state(self, updates):
        state = self.get_state()
        state.update(updates)
        self.save_state(state)
        return state

    def clear_state(self):
        state = copy.deepcopy(self.default_state)
        self.save_state(state)
        logger.info("Reset state for session %s", self.session.pk)

    def get(self, key, default=None):
        return self.get_state().get(key, default)

    def set(self, key, value):
        state = self.get_state()
        state[key] = value
        self.save_state(state)

    def __getitem__(self, key):
        return self.get_state()[key]

    def __setitem__(self, key, value):
        self.set(key, value)

    def __delitem__(self, key):
        state = self.get_state()
        del state[key]
        self.save_state(state)

    def mutate(self, mutator):
        state = self.get_state()
        mutator(state)
        self.save_state(state)
        return state

    def __contains__(self, key):
        return key in self.get_state()

    def keys(self):
        return self.get_state().keys()

    def items(self):
        return self.get_state().items()

    def get_step_label(self):
        step = self.get('current_step', 0)
        if 0 <= step < len(self.STEP_LABELS):
            return self.STEP_LABELS[step]
        return 'Unknown'
