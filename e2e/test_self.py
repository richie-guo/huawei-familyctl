#!/usr/bin/env python3
"""两个协议的自洽性自测：纯 Python 客户端/服务端对跑完整握手 + 加解密。
（与真实路由器/云端的互通还需传输层与凭证——见 docs/poc-2026-10-11-e2e-reversed.md 的缺口清单）"""
import json
import sys

from hichain_speke import SpekeClient, SpekeServer, envelope_encrypt, envelope_decrypt
from sts_channel import StsClient, StsServer


def test_speke():
    pin = "12345678"
    client = SpekeClient(pin)
    req = client.build_request()
    server = SpekeServer(pin, req)
    resp = server.build_response({"sessionId": "test-sid", "result": 0})
    ack = client.complete(resp, {})
    assert ack["data"]
    # 服务端能解开客户端回执
    server_ack = server.open(ack["data"])
    assert isinstance(server_ack, dict)
    # 双向业务报文加解密
    blob = envelope_encrypt(client.shared_key, json.dumps({"echo": "pong"}).encode())
    assert json.loads(envelope_decrypt(server.shared_key, blob)) == {"echo": "pong"}
    print("✅ SPEKE 握手+加解密 自洽（8位PIN / RFC5054-8192 / HKDF / AES-GCM信封）")


def test_sts():
    client = StsClient()
    req = client.build_public_key_request()
    server = StsServer(req)
    resp = server.build_public_key_response({"auth": "ok"})
    client.deal_public_key_response(resp)
    sig_req = client.build_signature_request({"ready": True})
    got = server.open(sig_req["data"])
    assert got == {"ready": True}
    sig_resp = server.build_public_key_response({})  # 复用构造器生成 {data,...}
    blob = envelope_encrypt(client.shared_key, b'{"final":1}')
    assert json.loads(envelope_decrypt(server.shared_key, blob)) == {"final": 1}
    print("✅ STS 握手+加解密 自洽（X25519 + Ed25519 + HKDF-16B + AES-GCM信封）")


if __name__ == "__main__":
    test_speke()
    test_sts()
    print("全部自测通过")
