from django.urls import path

from ledger import views


app_name = 'ledger'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('admin-panel/', views.admin_panel, name='admin_panel'),
    path('admin-panel/users/', views.admin_users, name='admin_users'),
    path('admin-panel/users/<int:pk>/edit/', views.admin_user_update, name='admin_user_update'),
    path('admin-panel/users/<int:pk>/reset-password/', views.admin_user_reset_password, name='admin_user_reset_password'),
    path('admin-panel/profiles/', views.admin_profiles, name='admin_profiles'),
    path('admin-panel/profiles/<int:pk>/edit/', views.admin_profile_update, name='admin_profile_update'),
    path('admin-panel/profiles/<int:pk>/check-telegram/', views.admin_profile_check_telegram, name='admin_profile_check_telegram'),
    path('admin-panel/categories/', views.admin_categories, name='admin_categories'),
    path('admin-panel/categories/new/', views.admin_category_create, name='admin_category_create'),
    path('admin-panel/categories/<int:pk>/edit/', views.admin_category_update, name='admin_category_update'),
    path('profile/', views.profile, name='profile'),
    path('planner/', views.planner, name='planner'),
    path('obligations/', views.obligation_list, name='obligation_list'),
    path('obligations/new/', views.obligation_create, name='obligation_create'),
    path('obligations/<int:pk>/', views.obligation_detail, name='obligation_detail'),
    path('obligations/<int:pk>/recalculate/', views.obligation_recalculate, name='obligation_recalculate'),
    path('obligations/<int:pk>/history/', views.obligation_history, name='obligation_history'),
    path('obligations/<int:pk>/history/accounting/', views.obligation_accounting_history, name='obligation_accounting_history'),
    path('obligations/<int:pk>/repay/', views.repayment_create, name='repayment_create'),
    path('obligations/<int:pk>/manual-transfers/<int:event_pk>/edit/', views.manual_transfer_update, name='manual_transfer_update'),
    path('obligations/<int:pk>/charges/new/', views.recurring_charge_create, name='recurring_charge_create'),
    path('obligations/<int:pk>/charges/<int:series_pk>/edit/', views.recurring_series_update, name='recurring_series_update'),
    path('obligations/<int:pk>/charges/generate-due/', views.recurring_due_generate, name='recurring_due_generate'),
    path('obligations/<int:pk>/charges/recalculate/', views.recurring_recalculate, name='recurring_recalculate'),
    path('obligations/<int:pk>/rates/new/', views.interest_rate_create, name='interest_rate_create'),
    path('obligations/<int:pk>/rates/<int:rate_pk>/edit/', views.interest_rate_update, name='interest_rate_update'),
    path('obligations/<int:pk>/interest/generate-due/', views.interest_due_generate, name='interest_due_generate'),
    path('obligations/<int:pk>/interest/recalculate/', views.interest_recalculate, name='interest_recalculate'),
    path('obligations/<int:pk>/close/', views.obligation_close, name='obligation_close'),
]
