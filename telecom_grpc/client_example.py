"""Minimal async gRPC client; use channel credentials in production."""
import asyncio

import grpc

from telecom_grpc import telecom_pb2, telecom_pb2_grpc


async def main(target: str, phone: str, ca_file: str, cert_file: str, key_file: str):
    credentials = grpc.ssl_channel_credentials(
        root_certificates=open(ca_file, "rb").read(),
        private_key=open(key_file, "rb").read(),
        certificate_chain=open(cert_file, "rb").read())
    async with grpc.aio.secure_channel(target, credentials,
        options=[("grpc.enable_retries", 0), ("grpc.keepalive_time_ms", 30000)]) as channel:
        stub = telecom_pb2_grpc.CallPolicyStub(channel)
        reply = await stub.CheckCallAllowed(telecom_pb2.CheckCallRequest(phone_number=phone), timeout=0.030)
        return reply


if __name__ == "__main__":
    print(asyncio.run(main("telecom-grpc.internal:50051", "+84901234567",
                           "client-ca.crt", "client.crt", "client.key")))
