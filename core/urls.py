from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

app_name = 'core'

urlpatterns = [
    # Authentication URLs
    path('login/', auth_views.LoginView.as_view(template_name='registration/login.html'), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),

    # Session management
    path('sessions/', views.session_hub, name='session_hub'),
    path('sessions/new/', views.create_session, name='create_session'),
    path('sessions/<int:session_id>/resume/', views.resume_session, name='resume_session'),
    path('sessions/<int:session_id>/', views.session_review, name='session_review'),
    path(
        'sessions/<int:session_id>/image/<path:filename>/',
        views.serve_session_image,
        name='serve_session_image',
    ),

    # Main application URLs
    path('start/', views.start_page, name='start_page'),
    path('step/<int:step_number>/', views.step_view, name='step_view'),
    path('upload-files/', views.upload_files, name='upload_files'),
    path('restart/', views.restart, name='restart'),
    path('get-step-template/<int:step_number>/', views.get_step_template, name='get_step_template'),
    path('select-file/', views.select_file, name='select_file'),
    path('image/<path:filename>/', views.serve_image, name='serve_image'),
    path('extract-image-data/', views.extract_image_data, name='extract_image_data'),
    path('save-extraction/', views.save_extraction, name='save_extraction'),
    path('confirm-extraction/', views.confirm_extraction, name='confirm_extraction'),
    path('clear-selection/', views.clear_selection, name='clear_selection'),
    path('start-sorting/', views.start_sorting, name='start_sorting'),
    path('assign-item/', views.assign_item, name='assign_item'),
    path('get-current-item/', views.get_current_sort_item, name='get_current_sort_item'),
    path('start-extraction/', views.start_extraction, name='start_extraction'),
    path('extract-current-image/', views.extract_current_image, name='extract_current_image'),
    path('skip-current-file/', views.skip_current_file, name='skip_current_file'),
    path('next-file/', views.next_file, name='next_file'),
]
