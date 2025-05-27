from django.apps import AppConfig

class DjangoScrapConfig(AppConfig):
    name = 'djangoscrap'

    def ready(self):
        import djangoscrap.signals
