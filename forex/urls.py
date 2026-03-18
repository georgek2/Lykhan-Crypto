from django.urls import path
from forex.views import SignalWebhookView, dashboard_view

urlpatterns = [
    path("webhook/signal/", SignalWebhookView.as_view(), name="forex-signal-webhook"),
    path("dashboard/",      dashboard_view,              name="forex-dashboard"),
]