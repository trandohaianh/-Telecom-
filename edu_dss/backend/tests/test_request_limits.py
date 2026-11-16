import unittest

from app.request_limits import RequestBodyLimitMiddleware


class RequestBodyLimitTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, messages, *, headers=(), limit=5):
        seen_by_app = []
        async def app(scope, receive, send):
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    break
                body.extend(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            seen_by_app.append(bytes(body))
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok", "more_body": False})

        incoming = iter(messages)
        async def receive():
            return next(incoming, {"type": "http.disconnect"})

        outgoing = []
        async def send(message):
            outgoing.append(message)

        middleware = RequestBodyLimitMiddleware(app, limit)
        await middleware({"type": "http", "method": "POST", "headers": list(headers)}, receive, send)
        return seen_by_app, outgoing

    async def test_chunked_body_at_limit_is_replayed_once(self):
        seen, sent = await self.run_case([
            {"type": "http.request", "body": b"ab", "more_body": True},
            {"type": "http.request", "body": b"cde", "more_body": False},
        ])
        self.assertEqual(seen, [b"abcde"])
        self.assertEqual(sent[0]["status"], 200)

    async def test_chunked_oversize_is_rejected_before_route(self):
        seen, sent = await self.run_case([
            {"type": "http.request", "body": b"abc", "more_body": True},
            {"type": "http.request", "body": b"def", "more_body": False},
        ])
        self.assertEqual(seen, [])
        self.assertEqual(sent[0]["status"], 413)

    async def test_declared_oversize_is_rejected_without_reading_body(self):
        seen, sent = await self.run_case(
            [], headers=((b"content-length", b"6"),))
        self.assertEqual(seen, [])
        self.assertEqual(sent[0]["status"], 413)


if __name__ == "__main__":
    unittest.main()
