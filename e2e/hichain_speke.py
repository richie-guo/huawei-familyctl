#!/usr/bin/env python3
"""hichain SPEKE（华为 IoT 设备认证栈 trustconnalg）——纯 Python 复刻。

来源：智慧生活 APK classes23.dex 反编译（2026-10-11），
对应 com/huawei/iotplatform/security/trustconnalg/{internal/pin/*, openapi/speke/*, openapi/crypto/*}。

协议（全部自字节码实证）：
  hashPin   = HKDF-SHA256(ikm=pinCode UTF-8(8字节), salt=salt, info=b"hichain_speke_base_info\\0")
  pubA      = (hashPin^2 mod p)^a mod p          # a=随机大整数（Java BigInteger(随机32字节)）
  z         = pubB^a mod p                        # Java BigInteger(byte[]) 带符号！
  sharedKey = HKDF-SHA256(ikm=z.toByteArray()[1:], salt=salt, info=b"hichain_speke_sessionkey_info\\0")
              # toByteArray 带符号首字节剥除（SHARE_KEY_START_INDEX=1）
  会话加密  = AES-GCM: b"\\x00" + nonce(12) + ct||tag(16)，密钥=sharedKey（32B→AES-256）
  消息      = {publicKey: b64, salt: b64, encryptedData: b64(aes(sharedKey, {businessData:...} JSON))}
  p         = RFC5054 8192-bit MODP 群
"""
import base64
import hashlib
import hmac as hmac_mod
import json
import os

P = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B"
    "302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7EDEE386BFB5A899FA5AE9F24117C4B1FE6"
    "49286651ECE45B3DC2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F83655D23DCA3AD961C62F356208552BB9ED529077096966D"
    "670C354E4ABC9804F1746C08CA18217C32905E462E36CE3BE39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF695581718"
    "3995497CEA956AE515D2261898FA051015728E5A8AAAC42DAD33170D04507A33A85521ABDF1CBA64ECFB850458DBEF0A8AEA71575D060C7D"
    "B3970F85A6E1E4C7ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6BF12FFA06D98A0864D87602733EC86A64521F2B18177B200C"
    "BBE117577A615D6C770988C0BAD946E208E24FA074E5AB3143DB5BFCE0FD108E4B82D120A93AD2CAFFFFFFFFFFFFFFFF", 16)

PIN_EXPAND_INFO = b"hichain_speke_base_info\x00"
SHARE_KEY_EXPAND_INFO = b"hichain_speke_sessionkey_info\x00"


def hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    prk = hmac_mod.new(salt or b"\x00" * 32, ikm, hashlib.sha256).digest()
    okm, t, i = b"", b"", 1
    while len(okm) < length:
        t = hmac_mod.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        okm += t
        i += 1
    return okm[:length]


def java_bytes(b: int) -> bytes:
    """Java BigInteger.toByteArray()：带符号补码，恒含符号字节"""
    length = b.bit_length() // 8 + 1
    return b.to_bytes(length, "big", signed=True)


def java_int(data: bytes) -> int:
    """Java new BigInteger(byte[])：带符号补码"""
    return int.from_bytes(data, "big", signed=True)


def hash_pin_code(pin_code: str, salt: bytes) -> bytes:
    assert len(pin_code) == 8, "pin code 长度必须为 8"
    return hkdf_sha256(pin_code.encode(), salt, PIN_EXPAND_INFO, 32)


def speke_public_key(hash_pin: bytes, private: int) -> bytes:
    base = pow(java_int(hash_pin) % P, 2, P)          # hashPinCodeModPrime：平方保证二次剩余
    return java_bytes(pow(base, private, P))


def speke_shared_key(peer_pub: bytes, private: int, salt: bytes) -> bytes:
    pub = java_int(peer_pub)
    if not (2 <= pub <= P - 2):
        raise ValueError("peer public key out of range")
    z = pow(pub % P, private, P)
    stripped = java_bytes(z)[1:]                       # 剥带符号首字节
    return hkdf_sha256(stripped, salt, SHARE_KEY_EXPAND_INFO, 32)


def envelope_encrypt(key: bytes, plaintext: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    return b"\x00" + nonce + AESGCM(key).encrypt(nonce, plaintext, None)


def envelope_decrypt(key: bytes, blob: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(blob) < 13 or blob[0] != 0:
        raise ValueError("版本不支持或长度不足")
    nonce, ct = blob[1:13], blob[13:]
    return AESGCM(key).decrypt(nonce, ct, None)


class SpekeClient:
    """发起方（App 侧）。startNegotiateSpeke → 服务器回 {publicKey, encryptedData} → 完成。"""

    def __init__(self, pin_code: str, salt: bytes | None = None):
        self.salt = salt or os.urandom(16)
        self.hash_pin = hash_pin_code(pin_code, self.salt)
        self.private = java_int(os.urandom(32))
        self.public_key = speke_public_key(self.hash_pin, self.private)
        self.shared_key = None

    def build_request(self) -> dict:
        return {"publicKey": base64.b64encode(self.public_key).decode(),
                "salt": base64.b64encode(self.salt).decode(), "extra": ""}

    def complete(self, response: dict, business_request: dict) -> dict:
        peer_pub = base64.b64decode(response["publicKey"])
        self.shared_key = speke_shared_key(peer_pub, self.private, self.salt)
        business = json.loads(envelope_decrypt(self.shared_key, base64.b64decode(response["encryptedData"])))
        reply_plain = json.dumps({"businessData": business.get("businessData", "")}, separators=(",", ":"))
        return {"data": base64.b64encode(envelope_encrypt(self.shared_key, reply_plain.encode())).decode()}

    def open(self, data_b64: str) -> dict:
        return json.loads(envelope_decrypt(self.shared_key, base64.b64decode(data_b64)))


class SpekeServer:
    """响应方（设备/路由器侧），用于自测对跑。"""

    def __init__(self, pin_code: str, request: dict):
        self.salt = base64.b64decode(request["salt"])
        self.hash_pin = hash_pin_code(pin_code, self.salt)
        self.private = java_int(os.urandom(32))
        self.public_key = speke_public_key(self.hash_pin, self.private)
        peer_pub = base64.b64decode(request["publicKey"])
        self.shared_key = speke_shared_key(peer_pub, self.private, self.salt)

    def build_response(self, business: dict) -> dict:
        plain = json.dumps({"businessData": json.dumps(business)}, separators=(",", ":"))
        return {"publicKey": base64.b64encode(self.public_key).decode(),
                "encryptedData": base64.b64encode(envelope_encrypt(self.shared_key, plain.encode())).decode()}

    def open(self, data_b64: str) -> dict:
        return json.loads(envelope_decrypt(self.shared_key, base64.b64decode(data_b64)))
