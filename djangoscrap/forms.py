from django import forms
from .models import Composition,Source

PREDEFINED_TYPES = [
    ('Instagram-tag', 'Instagram tag'),
    ('Instagram-account', 'Instagram account'),
    ('Instagram-location', 'Instagram location'),
    ('Yandex-tag', 'Yandex Tag'),
    ('Bing-tag', 'Bing Tag'),
    ('Tumblr', 'Tumblr'),
    ('Other', 'Other (enter manually)'),
]

class CompositionForm(forms.ModelForm):
    class Meta:
        model = Composition
        fields = ['type', 'background_video', 'foreground_video', 'audio_file']

class SourceForm(forms.ModelForm):
    custom_type = forms.CharField(required=False, label="Custom Type")

    class Meta:
        model = Source
        fields = ['name', 'type', 'source_id', 'last_scraped', 'query_no', 'initial', 'max_num']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'type': forms.Select(choices=PREDEFINED_TYPES, attrs={'class': 'form-control'}),
            'source_id': forms.TextInput(attrs={'class': 'form-control'}),
            'last_scraped': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'query_no': forms.TextInput(attrs={'class': 'form-control'}),
            'initial': forms.NumberInput(attrs={'class': 'form-control'}),
            'max_num': forms.NumberInput(attrs={'class': 'form-control'}),
        }

    def clean(self):
        cleaned_data = super().clean()
        source_type = cleaned_data.get('type')
        custom_type = cleaned_data.get('custom_type')

        if source_type == 'Other' and not custom_type:
            self.add_error('custom_type', "Please enter a custom type.")

        # Override 'type' field with the custom one if needed
        cleaned_data['type'] = custom_type if source_type == 'Other' else source_type
        return cleaned_data