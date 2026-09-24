import unittest

from ichancy_provider import IchancyAgentClient, IchancyAuthError


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self):
        self.calls = []
        self.responses = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class ProviderTests(unittest.TestCase):
    def test_sign_in_and_read_balance(self):
        session = FakeSession()
        session.responses = [
            FakeResponse({"status": True, "result": {"accessToken": "a", "refreshToken": "r"}}),
            FakeResponse({"status": True, "result": [{"balance": 12.5, "currencyCode": "EUR", "main": True}]}),
        ]
        client = IchancyAgentClient("agent", "secret", session=session)
        result = client.get_player_balance("123")
        self.assertEqual(result["result"][0]["balance"], 12.5)
        self.assertEqual(session.calls[0][0], "https://agents.ichancy.com/global/api/UserApi/signIn")
        self.assertEqual(session.calls[1][0], "https://agents.ichancy.com/global/api/UserApi/getPlayerBalanceById")
        self.assertEqual(session.calls[1][1]["headers"]["Authorization"], "Bearer a")

    def test_expired_envelope_refreshes_once_and_retries(self):
        session = FakeSession()
        session.responses = [
            FakeResponse({"status": True, "result": {"accessToken": "a1", "refreshToken": "r1"}}),
            FakeResponse({"status": True, "result": "ex"}),
            FakeResponse({"status": True, "result": {"accessToken": "a2", "refreshToken": "r2"}}),
            FakeResponse({"status": True, "result": [{"balance": 9, "currencyCode": "EUR", "main": True}]}),
        ]
        client = IchancyAgentClient("agent", "secret", session=session)
        result = client.get_player_balance("123")
        self.assertEqual(result["result"][0]["balance"], 9)
        self.assertEqual(len(session.calls), 4)
        self.assertEqual(session.calls[2][0], "https://agents.ichancy.com/global/api/UserApi/refreshToken")

    def test_missing_credentials_fails_without_network_call(self):
        session = FakeSession()
        client = IchancyAgentClient("", "", session=session)
        with self.assertRaises(IchancyAuthError):
            client.get_players()
        self.assertEqual(session.calls, [])


if __name__ == "__main__":
    unittest.main()
