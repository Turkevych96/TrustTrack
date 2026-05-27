from django.urls import path

from ledger import views


app_name = 'ledger'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('obligations/', views.obligation_list, name='obligation_list'),
    path('obligations/new/', views.obligation_create, name='obligation_create'),
    path('obligations/<int:pk>/', views.obligation_detail, name='obligation_detail'),
    path('obligations/<int:pk>/repay/', views.repayment_create, name='repayment_create'),
    path('obligations/<int:pk>/charges/new/', views.recurring_charge_create, name='recurring_charge_create'),
    path('obligations/<int:pk>/rates/new/', views.interest_rate_create, name='interest_rate_create'),
]
