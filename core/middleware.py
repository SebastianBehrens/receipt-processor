"""
Custom middleware for handling Authelia authentication headers.
"""
import logging
from django.contrib.auth.middleware import RemoteUserMiddleware
from django.contrib.auth import get_user_model
from django.conf import settings

logger = logging.getLogger(__name__)
User = get_user_model()


class AutheliaRemoteUserMiddleware(RemoteUserMiddleware):
    """
    Custom middleware to handle additional user attributes from Authelia headers.
    
    This middleware extends Django's RemoteUserMiddleware to populate user
    profiles with additional information sent by Authelia via HTTP headers.
    """
    
    def process_request(self, request):
        # Call parent method to handle basic remote user authentication
        result = super().process_request(request)
        
        # If user is authenticated, update additional attributes from headers
        if hasattr(request, 'user') and request.user.is_authenticated:
            self._update_user_from_headers(request)
        
        return result
    
    def _update_user_from_headers(self, request):
        """
        Update user attributes from Authelia headers.
        """
        user = request.user
        updated = False
        
        # Map of header names to user attributes
        header_mapping = {
            'HTTP_X_AUTHELIA_EMAIL': 'email',
            'HTTP_X_AUTHELIA_NAME': 'first_name',
            'HTTP_X_AUTHELIA_SURNAME': 'last_name',
            'HTTP_X_AUTHELIA_GROUPS': None,  # Handle groups separately
        }
        
        # Update user attributes from headers
        for header, attr in header_mapping.items():
            if header in request.META and attr:
                header_value = request.META[header].strip()
                if header_value and getattr(user, attr, '') != header_value:
                    setattr(user, attr, header_value)
                    updated = True
                    logger.debug(f"Updated user {user.username} {attr}: {header_value}")
        
        # Handle groups (if needed for future role-based access)
        if 'HTTP_X_AUTHELIA_GROUPS' in request.META:
            groups = request.META['HTTP_X_AUTHELIA_GROUPS']
            logger.debug(f"User {user.username} groups: {groups}")
            # Store groups in session for potential use
            request.session['authelia_groups'] = groups.split(',') if groups else []
        
        # Save user if any attributes were updated
        if updated:
            try:
                user.save()
                logger.info(f"Updated user attributes for {user.username}")
            except Exception as e:
                logger.error(f"Failed to save user {user.username}: {e}")


class AutheliaHeadersMiddleware:
    """
    Middleware to log Authelia headers for debugging purposes.
    Enable this during development to see what headers Authelia is sending.
    """
    
    def __init__(self, get_response):
        self.get_response = get_response
    
    def __call__(self, request):
        # Log Authelia headers for debugging
        if settings.DEBUG:
            authelia_headers = {
                key: value for key, value in request.META.items()
                if key.startswith('HTTP_X_AUTHELIA_') or key == 'HTTP_REMOTE_USER'
            }
            if authelia_headers:
                logger.debug(f"Authelia headers: {authelia_headers}")
        
        response = self.get_response(request)
        return response 