from types import SimpleNamespace

from app import llm


class FakeIndex:
    def search(self, question, top_k=5):
        return [
            {
                "text": "Acme Support is available Monday to Friday, 9 AM to 6 PM IST.",
                "source": "sample_kb.md",
                "score": 0.83,
            }
        ]


class FakeChoice:
    def __init__(self):
        self.message = SimpleNamespace(content="We are available Monday to Friday, 9 AM to 6 PM IST.")


class FakeChatCompletions:
    @staticmethod
    def create(**kwargs):
        return SimpleNamespace(choices=[FakeChoice()])


class FakeClient:
    chat = SimpleNamespace(completions=FakeChatCompletions())


def test_answer_query_includes_full_text_in_sources(monkeypatch):
    monkeypatch.setattr(llm, "get_client", lambda: FakeClient())

    result = llm.answer_query(FakeIndex(), "What are your support hours?")

    assert result["answer"] == "We are available Monday to Friday, 9 AM to 6 PM IST."
    assert result["sources"][0]["text"] == "Acme Support is available Monday to Friday, 9 AM to 6 PM IST."
    assert result["sources"][0]["source"] == "sample_kb.md"
    assert result["sources"][0]["score"] == 0.83
