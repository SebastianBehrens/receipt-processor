"""
Custom authentication backend for Authelia header-based integration.
"""
import logging
from django.conf import settings
from django.contrib.auth.models import Group, User
from django.contrib.auth.backends import RemoteUserBackend

logger = logging.getLogger(__name__)


class AutheliaRemoteUserBackend(RemoteUserBackend):
    """
    Authentication backend for Authelia that handles user creation and updates from headers.
    
    This backend can be used in conjunction with the AutheliaRemoteUserMiddleware
    to handle authentication outside Django and update local user with external information
    (name, email and groups) provided by Authelia headers.

    Extends RemoteUserBackend (it creates the Django user if it does not exist),
    updating the user with the information received from the remote headers.

    Django user is only added to groups that already exist on the database (no groups are created).
    A settings variable can be used to exclude some groups when updating the user.
    """

    excluded_groups = set()
    if hasattr(settings, 'AUTHELIA_EXCLUDED_GROUPS'):
        excluded_groups = set(settings.AUTHELIA_EXCLUDED_GROUPS)

    # Authelia header mappings
    header_name = 'HTTP_REMOTE_NAME'
    header_groups = 'HTTP_REMOTE_GROUPS'
    header_email = 'HTTP_REMOTE_EMAIL'

    def authenticate(self, request, remote_user):
        """Authenticate user and update their information from Authelia headers."""
        if not remote_user:
            return None
            
        user = super().authenticate(request, remote_user)

        # original authenticate calls configure_user only
        # when user is created. We need to call this method every time
        # the user is authenticated in order to update its data.
        if user:
            logger.debug(f"Authenticating user: {user.username}")
            self.configure_user(request, user)
        return user

    def configure_user(self, request, user, created=None):
        """
        Complete the user from extra request.META information.
        Updates user data from Authelia headers.
        
        Args:
            request: The HTTP request object
            user: The Django user object
            created: Optional boolean indicating if user was just created (Django 4.2+)
        """
        logger.debug(f"Configuring user: {user.username} (created={created})")
        
        # Update display name from Remote-Name header
        if self.header_name in request.META:
            name = request.META[self.header_name]
            logger.debug(f"Got Remote-Name header: {name}")
            # Split name into first_name and last_name
            name_parts = name.split(' ', 1) if name else []
            user.first_name = name_parts[0] if name_parts else ''
            user.last_name = name_parts[1] if len(name_parts) > 1 else ''

        # Update email from Remote-Email header
        if self.header_email in request.META:
            user.email = request.META[self.header_email]
            logger.debug(f"Got Remote-Email header: {user.email}")

        # Update groups from Remote-Groups header
        if self.header_groups in request.META:
            groups = request.META[self.header_groups]
            logger.debug(f"Got Remote-Groups header: {groups}")
            self.update_groups(user, groups)

        # Set staff status based on groups or other logic
        if self.user_should_be_staff(user):
            user.is_staff = True

        user.save()
        logger.info(f"Updated user: {user.username} (email: {user.email})")
        return user

    def user_should_be_staff(self, user):
        """
        Determine if user should have staff privileges.
        Override this method to implement your own logic.
        """
        # Example: make users in 'admin' group staff
        return user.groups.filter(name='admin').exists()

    def update_groups(self, user, remote_groups):
        """
        Synchronizes groups the user belongs to with remote information.

        Groups (existing django groups or remote groups) on excluded_groups are completely ignored.
        No group will be created on the django database.

        Disclaimer: this method is inspired by the LDAPBackend from django-auth-ldap.
        """
        if not remote_groups:
            return

        current_group_names = frozenset(
            user.groups.values_list("name", flat=True).iterator()
        )
        preserved_group_names = current_group_names.intersection(self.excluded_groups)
        current_group_names = current_group_names - self.excluded_groups

        target_group_names = frozenset(
            [x for x in map(self.clean_groupname, remote_groups.split(',')) if x is not None]
        )
        target_group_names = target_group_names - self.excluded_groups

        if target_group_names != current_group_names:
            target_group_names = target_group_names.union(preserved_group_names)
            existing_groups = list(
                Group.objects.filter(name__in=target_group_names).iterator()
            )
            user.groups.set(existing_groups)
            logger.debug(f"Updated groups for {user.username}: {[g.name for g in existing_groups]}")

    def clean_groupname(self, groupname):
        """
        Perform any cleaning on the "groupname" prior to using it.
        Return the cleaned groupname.
        """
        return groupname.strip() if groupname else None 