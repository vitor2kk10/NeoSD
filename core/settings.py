"""Preferências do app: guia 'Primeiros passos' e opções do modo console/Windows."""
from .store import load_json, save_json

FILE = "library/app_settings.json"


class AppSettings:
    def __init__(self):
        self.data = load_json(FILE)

    @property
    def first_run(self):
        return not self.data.get("onboarding_done")

    def finish_onboarding(self):
        self.set("onboarding_done", True)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        save_json(FILE, self.data)
