from urllib import request

from django.shortcuts import render

# Create your views here.









def dashboard_view(request):
    from django.shortcuts import render
    return render(request, "dashboard/index.html")
    
