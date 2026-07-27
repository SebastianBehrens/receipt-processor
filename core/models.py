from django.contrib.auth.models import AbstractUser
from django.db import models
from django.core.cache import cache
import json
import logging

logger = logging.getLogger(__name__)

class User(AbstractUser):
    """Custom user model for receipt processor users."""
    display_name = models.CharField(max_length=100, blank=True)
    
    def __str__(self):
        return self.username

class UserState:
    """Manages user-specific state using Django cache."""
    
    def __init__(self, user_id):
        self.user_id = user_id
        self.cache_key = f"receipt_state_{user_id}"
        self.default_state = {
            'current_step': 0,
            'receipt_zip': None,
            'extracted_files': [],
            'selected_file': None,
            'payer': None,
            'consumption': {
                'sebastian': [],
                'iva': [],
                'both': []
            },
            'sort_items': [],
            'current_sort_index': 0,
            'api_costs_total': 0,
            'current_extraction_index': 0,
            'files_processed': 0,
            'progress_percentage': 0,
            'current_file': None,
            'extracted': {},
            'aggregation': {}
        }
    
    def get_state(self):
        """Get user state from cache or return default state."""
        state = cache.get(self.cache_key)
        if state is None:
            state = self.default_state.copy()
            self.save_state(state)
            logger.info(f"Created new state for user {self.user_id}")
        return state
    
    def save_state(self, state):
        """Save user state to cache with 24 hour timeout."""
        cache.set(self.cache_key, state, timeout=86400)  # 24 hours
        logger.debug(f"Saved state for user {self.user_id}")
    
    def update_state(self, updates):
        """Update specific keys in user state."""
        state = self.get_state()
        state.update(updates)
        self.save_state(state)
        return state
    
    def clear_state(self):
        """Clear user state (reset to default)."""
        cache.delete(self.cache_key)
        logger.info(f"Cleared state for user {self.user_id}")
    
    def get(self, key, default=None):
        """Get a specific key from user state."""
        state = self.get_state()
        return state.get(key, default)
    
    def set(self, key, value):
        """Set a specific key in user state."""
        state = self.get_state()
        state[key] = value
        self.save_state(state)
    
    def __getitem__(self, key):
        """Allow dict-like access to state."""
        return self.get_state()[key]
    
    def __setitem__(self, key, value):
        """Allow dict-like assignment to state."""
        self.set(key, value)
    
    def __contains__(self, key):
        """Allow 'in' operator with state."""
        return key in self.get_state()
    
    def keys(self):
        """Get all state keys."""
        return self.get_state().keys()
    
    def items(self):
        """Get all state items."""
        return self.get_state().items()
