"""
Custom middleware for handling Authelia authentication headers.
"""
import logging
from django.contrib.auth.middleware import RemoteUserMiddleware
from django.contrib.auth import get_user_model, login
from django.conf import settings

from .utils import get_active_session, get_user_state

logger = logging.getLogger(__name__)
User = get_user_model()


class AutheliaRemoteUserMiddleware(RemoteUserMiddleware):
    """
    Custom middleware to handle additional user attributes from Authelia headers.

    This middleware extends Django's RemoteUserMiddleware to populate user
    profiles with additional information sent by Authelia via HTTP headers.
    """

    def process_request(self, request):
        result = super().process_request(request)

        if hasattr(request, 'user') and request.user.is_authenticated:
            self._update_user_from_headers(request)

        return result

    def _update_user_from_headers(self, request):
        user = request.user
        updated = False

        header_mapping = {
            'HTTP_X_AUTHELIA_EMAIL': 'email',
            'HTTP_X_AUTHELIA_NAME': 'first_name',
            'HTTP_X_AUTHELIA_SURNAME': 'last_name',
            'HTTP_X_AUTHELIA_GROUPS': None,
        }

        for header, attr in header_mapping.items():
            if header in request.META and attr:
                header_value = request.META[header].strip()
                if header_value and getattr(user, attr, '') != header_value:
                    setattr(user, attr, header_value)
                    updated = True
                    logger.debug("Updated user %s %s: %s", user.username, attr, header_value)

        if 'HTTP_X_AUTHELIA_GROUPS' in request.META:
            groups = request.META['HTTP_X_AUTHELIA_GROUPS']
            logger.debug("User %s groups: %s", user.username, groups)
            request.session['authelia_groups'] = groups.split(',') if groups else []

        if updated:
            try:
                user.save()
                logger.info("Updated user attributes for %s", user.username)
            except Exception as e:
                logger.error("Failed to save user %s: %s", user.username, e)


class AutheliaHeadersMiddleware:
    """Middleware to log Authelia headers for debugging purposes."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if settings.DEBUG:
            authelia_headers = {
                key: value for key, value in request.META.items()
                if key.startswith('HTTP_X_AUTHELIA_') or key == 'HTTP_REMOTE_USER'
            }
            if authelia_headers:
                logger.debug("Authelia headers: %s", authelia_headers)

        response = self.get_response(request)
        return response


class AutheliaMiddleware:
    """Middleware to handle Authelia authentication."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            remote_user = request.META.get('HTTP_REMOTE_USER')
            remote_name = request.META.get('HTTP_REMOTE_NAME', '')
            remote_email = request.META.get('HTTP_REMOTE_EMAIL', '')

            if remote_user:
                try:
                    user, created = User.objects.get_or_create(
                        username=remote_user,
                        defaults={
                            'first_name': remote_name.split(' ')[0] if remote_name else '',
                            'last_name': ' '.join(remote_name.split(' ')[1:]) if remote_name else '',
                            'email': remote_email,
                            'is_active': True,
                        }
                    )

                    if created:
                        logger.info("Created new user: %s", remote_user)

                    if not created:
                        updated = False
                        if remote_name and user.get_full_name() != remote_name:
                            names = remote_name.split(' ')
                            user.first_name = names[0] if names else ''
                            user.last_name = ' '.join(names[1:]) if len(names) > 1 else ''
                            updated = True
                        if remote_email and user.email != remote_email:
                            user.email = remote_email
                            updated = True
                        if updated:
                            user.save()

                    login(request, user)
                    logger.info("Authenticated user via Authelia: %s", remote_user)

                except Exception as e:
                    logger.error("Error authenticating user %s: %s", remote_user, e)

        response = self.get_response(request)
        return response


class UserStateMiddleware:
    """Attach per-user workflow state when an active session is selected."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.active_session = None
        request.user_state = None
        if request.user.is_authenticated:
            session = get_active_session(request)
            if session:
                request.active_session = session
                request.user_state = get_user_state(request)
        return self.get_response(request)
