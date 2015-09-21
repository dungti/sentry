from __future__ import absolute_import

from django import forms
from django.http import HttpResponse
from django.views.generic import View
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe
from django.views.decorators.csrf import csrf_exempt

from sentry.models import Group, ProjectKey, UserReport, UserReportResolution
from sentry.web.helpers import render_to_response
from sentry.utils import json
from sentry.utils.http import is_valid_origin


class UserReportForm(forms.ModelForm):
    name = forms.CharField(max_length=128, widget=forms.TextInput(attrs={
        'placeholder': 'Jane Doe',
    }))
    email = forms.EmailField(max_length=75, widget=forms.TextInput(attrs={
        'placeholder': 'jane@example.com',
        'type': 'email',
    }))
    comments = forms.CharField(widget=forms.Textarea(attrs={
        'placeholder': "I clicked on 'X' and then hit 'Confirm'",
    }))
    notifyme = forms.BooleanField(required=False)

    class Meta:
        model = UserReport
        fields = ('name', 'email', 'comments')


class ErrorPageEmbedView(View):
    def _get_project_key(self, request):
        dsn = request.GET.get('dsn')
        try:
            key = ProjectKey.from_dsn(dsn)
        except ProjectKey.DoesNotExist:
            return

        return key

    def _get_origin(self, request):
        return request.META.get('HTTP_ORIGIN', request.META.get('HTTP_REFERER'))

    def _json_response(self, request, context=None, status=200):
        if context:
            content = json.dumps(context)
        else:
            content = ''
        response = HttpResponse(content, status=status, content_type='application/json')
        response['Access-Control-Allow-Origin'] = request.META.get('HTTP_ORIGIN', '')
        response['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response['Access-Control-Max-Age'] = '1000'
        response['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Requested-With'
        return response

    @csrf_exempt
    def dispatch(self, request):
        try:
            event_id = request.GET['eventId']
        except KeyError:
            return self._json_response(request, status=400)

        key = self._get_project_key(request)
        if not key:
            return self._json_response(request, status=404)

        origin = self._get_origin(request)
        if not origin:
            return self._json_response(request, status=403)

        if not is_valid_origin(origin, key.project):
            return HttpResponse(status=403)

        if request.method == 'OPTIONS':
            return self._json_response(request)

        # TODO(dcramer): since we cant use a csrf cookie we should at the very
        # least sign the request / add some kind of nonce
        initial = {
            'name': request.GET.get('name'),
            'email': request.GET.get('email'),
            'notifyme': True,
        }

        form = UserReportForm(request.POST if request.method == 'POST' else None,
                              initial=initial)
        if form.is_valid():
            report = form.save(commit=False)
            if form.cleaned_data.get('notifyme'):
                report.resolution = UserReportResolution.AWAITING_RESOLUTION
            report.project = key.project
            report.event_id = event_id
            try:
                report.group = Group.objects.get(
                    eventmapping__event_id=report.event_id,
                    eventmapping__project=key.project,
                )
            except Group.DoesNotExist:
                # XXX(dcramer): the system should fill this in later
                pass
            report.save()
            return HttpResponse(status=200)
        elif request.method == 'POST':
            return self._json_response(request, {
                "errors": dict(form.errors),
            }, status=400)

        template = render_to_string('sentry/error-page-embed.html', {
            'form': form,
        })

        context = {
            'endpoint': mark_safe(json.dumps(request.get_full_path())),
            'template': mark_safe(json.dumps(template)),
        }

        return render_to_response('sentry/error-page-embed.js', context, request,
                                  content_type='text/javascript')
