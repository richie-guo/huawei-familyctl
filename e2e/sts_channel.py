#!/usr/bin/env python3
"""STS 通道（trustconnalg openapi/sts）——纯 Python 复刻。

对应 StsJsonClient/StsProtocolImpl（classes23.dex 反编译，2026-10-11）。
设备档案里的 stsSession{supportE2e, e2eCtrlState} 即此协议的会话/状态。

握手（三轮 JSON 消息，字段 {publicKey, salt, extra} / {data, signature, extra}）：
  ① 客户端 生成 X25519 密钥对 → {publicKey: X25519pub, salt: 16B}
  ② 服务端 回 {publicKey: 对端X25519pub, data: aes(共享密钥, ...), signature: 对端Ed25519签名}
     sharedKey = HKDF-SHA256(ikm=X25519(myPriv, peerPub), salt=salt, info=空, len=16)
  ③ 客户端 回 {signature: Ed25519(edPriv, 自己的X25519pub), data: aes(...)}
  会话加密与 SPEKE 相同信封：b"\\x00" + nonce12 + ct||tag（AES-128-GCM，密钥16B）

⚠️ edPrivateKey 从外部注入（App 侧来源未定位——待查缺口）；ikm/salt 参数序按字节码推断。
"""
import base64
import json
import os

from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from hichain_speke import hkdf_sha256, envelope_encrypt, envelope_decrypt


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _unb64(s: str) -> bytes:
    return base64.b64decode(s)


class StsClient:
    """buildPublicKeyRequest → dealPublicKeyResponse → buildSignatureRequest → dealSignatureResponse"""

    def __init__(self, ed_private: bytes | None = None):
        self.x = X25519PrivateKey.generate()
        self.ed = Ed25519PrivateKey.from_private_bytes(ed_private) if ed_private else Ed25519PrivateKey.generate()
        self.salt = os.urandom(16)
        self.shared_key = None
        self.peer_ed_public = None

    def build_public_key_request(self) -> dict:
        pub = self.x.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return {"publicKey": _b64(pub), "salt": _b64(self.salt), "extra": ""}

    def _my_pub(self) -> bytes:
        return self.x.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    def deal_public_key_response(self, resp: dict) -> dict:
        peer_pub = _unb64(resp["publicKey"])
        salt = _unb64(resp.get("extra", "")) or self.salt
        z = self.x.exchange(X25519PublicKey.from_public_bytes(peer_pub))
        self.shared_key = hkdf_sha256(z, salt, b"", 16)
        self.peer_ed_public = _unb64(resp.get("signature", "")) or None
        inner = envelope_decrypt(self.shared_key, _unb64(resp["data"]))
        return json.loads(inner.decode())

    def build_signature_request(self, payload: dict) -> dict:
        sig = self.ed.sign(self._my_pub())
        plain = json.dumps(payload, separators=(",", ":")).encode()
        return {"signature": _b64(sig), "data": _b64(envelope_encrypt(self.shared_key, plain)), "extra": ""}

    def deal_signature_response(self, resp: dict) -> dict:
        return json.loads(envelope_decrypt(self.shared_key, _unb64(resp["data"])).decode())

    def open(self, data_b64: str) -> dict:
        return json.loads(envelope_decrypt(self.shared_key, _unb64(data_b64)).decode())


class StsServer:
    """服务端角色，自测对跑用。"""

    def __init__(self, request: dict):
        self.x = X25519PrivateKey.generate()
        self.ed = Ed25519PrivateKey.generate()
        self.salt = os.urandom(16)
        peer_pub = _unb64(request["publicKey"])
        peer_salt = _unb64(request.get("salt", "")) or self.salt
        z = self.x.exchange(X25519PublicKey.from_public_bytes(peer_pub))
        self.shared_key = hkdf_sha256(z, peer_salt, b"", 16)

    def build_public_key_response(self, payload: dict) -> dict:
        pub = self.x.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        sig = self.ed.sign(pub)
        plain = json.dumps(payload, separators=(",", ":")).encode()
        return {"publicKey": _b64(pub), "signature": _b64(sig),
                "data": _b64(envelope_encrypt(self.shared_key, plain)), "extra": ""}

    def open(self, data_b64: str) -> dict:
        return json.loads(envelope_decrypt(self.shared_key, _unb64(data_b64)).decode())
