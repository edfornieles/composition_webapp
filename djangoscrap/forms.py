from django import forms
from .models import Composition,Bucket

class CompositionForm(forms.ModelForm):
    class Meta:
        model = Composition
        fields = ['type', 'background_video', 'foreground_video', 'audio_file']

class BucketForm(forms.ModelForm):
    class Meta:
        model = Bucket
        fields = '__all__'