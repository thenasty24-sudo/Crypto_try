"""Generates the Ed25519 key pair for Robinhood's Crypto Trading API."""
import base64
from cryptography.hazmat.primitives import serialization as s
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

k = Ed25519PrivateKey.generate()
priv = k.private_bytes(s.Encoding.Raw, s.PrivateFormat.Raw, s.NoEncryption())
pub = k.public_key().public_bytes(s.Encoding.Raw, s.PublicFormat.Raw)
print("private:", base64.b64encode(priv).decode())
print("public: ", base64.b64encode(pub).decode())
