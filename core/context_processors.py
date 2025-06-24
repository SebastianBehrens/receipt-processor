import os
from django.conf import settings

def version_context(request):
    """Add APPLICATION_VERSION to template context."""
    return {
        'APPLICATION_VERSION': os.getenv('APPLICATION_VERSION')
    } 