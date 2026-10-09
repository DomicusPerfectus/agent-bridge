"""Hermes envelope codec; identity/registration do not imply availability."""

from .base import GenericAdapter


class HermesAdapter(GenericAdapter):
    name = "hermes"
