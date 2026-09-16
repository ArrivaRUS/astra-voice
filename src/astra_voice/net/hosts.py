"""Разрешённые источники моделей без зависимости от HTTP-транспорта."""

ALLOWED_HOSTS = (
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "huggingface.co",
    "hf.co",
)
_SUBDOMAIN_HOSTS = ("huggingface.co", "hf.co")


def host_allowed(host: str, allowed_hosts: tuple[str, ...] = ALLOWED_HOSTS) -> bool:
    """Сравнивает метки домена; поддомены разрешены только для двух источников."""
    labels = host.lower().split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in label)
        for label in labels
    ):
        return False
    for allowed in allowed_hosts:
        allowed_labels = allowed.split(".")
        if labels == allowed_labels:
            return True
        if allowed in _SUBDOMAIN_HOSTS and labels[-len(allowed_labels) :] == allowed_labels:
            return True
    return False
