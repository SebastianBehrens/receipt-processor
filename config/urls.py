"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.contrib import admin
from django.urls import path, include
from django.contrib.auth import views as auth_views
from core.views import start_page, custom_login, health_check
from django.shortcuts import redirect

urlpatterns = [
    # Root URL - show login page
    path('', custom_login, name='root'),
    
    # Public routes - accessible without Authelia authentication
    path("admin/", admin.site.urls),  # This will use Django's default admin interface
    path("accounts/login/", custom_login, name="login"),
    path("accounts/", include("django.contrib.auth.urls")),
    path("health/", health_check, name="health_check"),  # Health check endpoint
    
    # Protected routes - these should be protected by Authelia in your proxy config
    path("app/", start_page, name="start_page"),  # Main application entry point
    path("app/core/", include("core.urls")),  # All core application routes
]
