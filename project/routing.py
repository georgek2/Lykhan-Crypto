"""project/routing.py"""
from django.urls import re_path
from forex.consumers import DashboardConsumer
 
websocket_urlpatterns = [
    re_path(r"ws/dashboard/$", DashboardConsumer.as_asgi()),
]
 