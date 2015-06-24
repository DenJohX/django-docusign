"""Demo views for `django-docusign`."""
import os

from django.core.files.base import ContentFile
from django.core.urlresolvers import reverse
from django.views.generic import FormView, TemplateView, RedirectView
from django.views.generic.detail import SingleObjectMixin
from django.utils.text import slugify
from django.utils.timezone import now

import django_anysign
import django_docusign

from django_docusign_demo import forms
from django_docusign_demo import models


def docusign_settings(request):
    """Return dictionary of credentials for DocuSign from session or environ.

    Values are read from session, and fallback to environ.

    """
    data = {}
    for key in 'root_url', 'username', 'password', 'integrator_key', 'timeout':
        try:
            data[key] = request.session[key]
        except KeyError:
            pass
    return data


class HomeView(TemplateView):
    template_name = 'home.html'

    def get_context_data(self, **kwargs):
        data = super(HomeView, self).get_context_data(**kwargs)
        data['has_settings'] = all(docusign_settings(self.request))
        data['latest_signatures'] = models.Signature.objects \
            .all() \
            .order_by('-pk')[0:5]
        return data


class SettingsView(FormView):
    """Store DocuSign settings in session."""
    form_class = forms.SettingsForm
    template_name = 'settings.html'

    def form_valid(self, form):
        """Save configuration in session."""
        data = form.cleaned_data
        for (key, value) in data.items():
            if value:
                self.request.session[key] = value
        return super(SettingsView, self).form_valid(form)

    def get_success_url(self):
        return reverse('home')

    def get_initial(self):
        return docusign_settings(self.request)

    def get_context_data(self, **kwargs):
        data = super(SettingsView, self).get_context_data(**kwargs)
        data['default_backend'] = django_docusign.DocuSignBackend()
        return data


class CreateSignatureView(FormView):
    """Create DocuSign envelope."""
    form_class = forms.CreateSignatureForm
    template_name = 'create_signature.html'

    def get_success_url(self):
        """Return home URL."""
        return reverse('home')

    def form_valid(self, form):
        """Create envelope on DocuSign's side."""
        self.cleaned_data = form.cleaned_data
        # Prepare signature instance with uploaded document, Django side.
        (signature_type, created) = models.SignatureType.objects.get_or_create(
            signature_backend_code='docusign',
            docusign_template_id='')
        signature = models.Signature(
            signature_type=signature_type,
            document=self.request.FILES['document'],
            document_title=self.cleaned_data['title'],
        )
        signature.save()
        # Add signers.
        for position, signer_data in enumerate(self.cleaned_data['signers']):
            signature.signers.create(
                full_name=signer_data['name'],
                email=signer_data['email'],
                signing_order=position + 1,  # Position starts at 1.
                signature_backend_id=''
            )
        # Create signature, backend side.
        self.create_signature(signature)
        return super(CreateSignatureView, self).form_valid(form)

    @property
    def signature_backend(self):
        try:
            return self._signature_backend
        except AttributeError:
            self._signature_backend = self.get_signature_backend()
            return self._signature_backend

    def get_signature_backend(self):
        """Return signature backend instance."""
        backend_settings = docusign_settings(self.request)
        signature_backend = django_anysign.get_signature_backend(
            'docusign',
            **backend_settings
        )
        return signature_backend

    def create_signature(self, signature):
        """Create signature backend-side."""
        self.signature_backend.create_signature(
            signature,
            callback_url=self.cleaned_data['callback_url'],
            subject=signature.document_title,
        )


class CreateSignatureTemplateView(CreateSignatureView):
    """Create DocuSign envelope."""
    form_class = forms.CreateSignatureTemplateForm
    template_name = 'create_signature_template.html'

    def get_initial(self):
        """template_id initial value."""
        initial = self.initial.copy()
        initial['template_id'] = os.environ.get('DOCUSIGN_TEST_TEMPLATE_ID')
        return initial

    def form_valid(self, form):
        """Create envelope on DocuSign's side."""
        self.cleaned_data = form.cleaned_data
        # Prepare signature instance with uploaded document, Django side.
        (signature_type, created) = models.SignatureType.objects.get_or_create(
            signature_backend_code='docusign',
            docusign_template_id=self.cleaned_data['template_id'])
        signature = models.Signature.objects.create(
            signature_type=signature_type,
            document_title=self.cleaned_data['title'],
        )

        # Add signers.
        for position, signer_data in enumerate(self.cleaned_data['signers']):
            signature.signers.create(
                full_name=signer_data['name'],
                email=signer_data['email'],
                signing_order=position + 1,  # Position starts at 1.
            )
        # Create signature, backend side.
        self.create_signature(signature)
        return super(CreateSignatureView, self).form_valid(form)


class SignerView(SingleObjectMixin, RedirectView):
    """Embed DocuSign's recipient view."""
    model = models.Signer

    def get_redirect_url(self, *args, **kwargs):
        """Return URL where signer is redirected once doc has been signed."""
        signer = self.get_object()
        backend_settings = docusign_settings(self.request)
        signature_backend = django_anysign.get_signature_backend(
            'docusign',
            **backend_settings
        )
        signer_return_url = self.request.build_absolute_uri(
            signature_backend.get_signer_return_url(signer))
        url = signature_backend.post_recipient_view(
            signer, signer_return_url=signer_return_url)
        return url


class SignerReturnView(TemplateView):
    """Welcome the signer back from DocuSign."""
    template_name = 'signer_return.html'


class SignatureCallbackView(django_docusign.SignatureCallbackView):
    def get_signature_backend(self):
        """Return signature backend instance."""
        backend_settings = docusign_settings(self.request)
        signature_backend = django_anysign.get_signature_backend(
            'docusign',
            **backend_settings
        )
        return signature_backend

    def update_signer(self, signer_id, status, status_datetime=None,
                      message=u''):
        signer = django_anysign.get_signer_model().objects.get(pk=signer_id)
        signer.status = status
        signer.status_datetime = status_datetime or now()
        signer.status_details = message
        signer.save()

    def update_signature(self, status, status_datetime=None):
        self.signature.status = status
        self.signature.save()
        if status == 'completed' or not self.signature.document:
            document = self.signature_backend.get_docusign_documents(
                self.signature).next()  # In our model, there is only one doc.
            try:
                # Replace old document by signed one.
                filename = self.signature.document.name
                if not filename:
                    filename = "%s.pdf" % slugify(
                        self.signature.document_title)
                self.signature.document.delete(save=False)
                self.signature.document.save(filename,
                                             ContentFile(document.read()),
                                             save=True)
            finally:
                document.close()
