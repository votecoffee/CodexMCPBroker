"""Synthetic data only; no native applications, files or credentials."""

import base64

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7N8AAAAASUVORK5CYII="


def payload():
    return {
        "positive": 2**63 - 1,
        "negative": -(2**63),
        "unicode": "μ 深度 👩🏽‍💻",
        "nested": [None, True, False, 1.25, {"escaped": '"\\\n'}],
        "binary": base64.b64encode(bytes(range(256)) * 4096).decode("ascii"),
    }
