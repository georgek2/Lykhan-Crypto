from django.shortcuts import render

# Create your views here.


def home(request):

    context = {
        'dummy_value': 2026,
    }

    return render(request, 'agent/agent.html', context=context)




