"""
Custom authentication middleware for Authelia header-based integration.
"""
from django.contrib.auth.middleware import RemoteUserMiddleware, PersistentRemoteUserMiddleware


class AutheliaRemoteUserMiddleware(RemoteUserMiddleware):
    """
    Middleware for Authelia header-based authentication.
    Uses the Remote-User header set by Authelia proxy.
    """
    header = 'HTTP_REMOTE_USER'
    
    # Set to False if you don't want to create users automatically
    # create_unknown_user = False


class PersistentAutheliaRemoteUserMiddleware(PersistentRemoteUserMiddleware):
    """
    Persistent version that maintains the authenticated session until explicit logout.
    
    The RemoteUserMiddleware authentication middleware assumes that the HTTP request header 
    REMOTE_USER is present with all authenticated requests.

    With PersistentRemoteUserMiddleware, it is possible to receive this header only on a few 
    pages (as login page) and maintain the authenticated session until explicit 
    logout by the user.
    
    Security Warning: The proxy server MUST set Remote-User header EVERY TIME it hits 
    the Django application. If you only protect the login URL with Authelia and use 
    this persistent class, you have to set this header to '' on the other locations.
    """
    header = 'HTTP_REMOTE_USER' 