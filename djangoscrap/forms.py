from django import forms

from .models import Bucket, Composition, MintCollectionSettings, MonologuePersona


class MonologuePersonaForm(forms.ModelForm):
    """Dashboard form; `source_urls` stored as JSON — edited as one URL per line."""

    source_urls_text = forms.CharField(
        required=False,
        label="Reference URLs",
        widget=forms.Textarea(attrs={"rows": 5, "class": "form-control", "placeholder": "One URL per line (social, blogs, news…)"}),
    )

    class Meta:
        model = MonologuePersona
        fields = [
            "title",
            "slug",
            "character_name",
            "archetype",
            "source_urls_text",
            "style_corpus",
            "stream_seed_text",
            "system_prompt_hint",
            "typing_min_ms",
            "typing_max_ms",
            "pause_between_segments_ms",
            "is_published",
        ]
        widgets = {
            "title": forms.TextInput(attrs={"class": "form-control"}),
            "slug": forms.TextInput(attrs={"class": "form-control", "placeholder": "Leave blank to generate from title"}),
            "character_name": forms.TextInput(attrs={"class": "form-control"}),
            "archetype": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. jock, poet"}),
            "style_corpus": forms.Textarea(attrs={"rows": 6, "class": "form-control"}),
            "stream_seed_text": forms.Textarea(
                attrs={
                    "rows": 8,
                    "class": "form-control",
                    "placeholder": "Optional script: paragraphs separated by a blank line become segments.",
                }
            ),
            "system_prompt_hint": forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
            "typing_min_ms": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
            "typing_max_ms": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
            "pause_between_segments_ms": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
            "is_published": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            urls = self.instance.source_urls or []
            if isinstance(urls, list):
                self.initial["source_urls_text"] = "\n".join(str(u) for u in urls)

    def save(self, commit=True):
        obj = super().save(commit=False)
        text = self.cleaned_data.get("source_urls_text") or ""
        obj.source_urls = [u.strip() for u in text.splitlines() if u.strip()]
        if commit:
            obj.save()
        return obj


class CompositionForm(forms.ModelForm):
    class Meta:
        model = Composition
        fields = ['type', 'background_video', 'foreground_video', 'audio_file']


class MintCollectionSettingsForm(forms.ModelForm):
    class Meta:
        model = MintCollectionSettings
        fields = ["collection_title", "collection_icon"]
        widgets = {
            "collection_title": forms.TextInput(attrs={"class": "form-control"}),
            "collection_icon": forms.ClearableFileInput(attrs={"class": "form-control-file", "accept": "image/*"}),
        }


class BucketForm(forms.ModelForm):
    class Meta:
        model = Bucket
        fields = '__all__'