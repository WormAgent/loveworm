# 🪱 LoveWorm

**An open-source AI wingman that helps you date better — not more.**

LoveWorm helps you write a better profile, figure out what to say, and plan great dates. It suggests; you decide. It never talks to anyone on your behalf without your approval.

Part of the [<org>](https://github.com/<org>) family of open-source AI agents, alongside [WorkWorm](https://github.com/<org>/workworm) and [InvestWorm](https://github.com/<org>/investworm).

---

## Features

- ✍️ **Profile review** — feedback on your bio and photo order, with concrete rewrites
- 💬 **Conversation coach** — reply suggestions and openers based on the other person's profile
- 📍 **Date planner** — date ideas based on shared interests, budget and location
- 🧠 **Debrief** — reflect on how a date went and what to try next
- 🔌 **Bring your own model** — works with <Claude / OpenAI / local models>

> 🚧 Early development. Some features may be in progress.

## Principles

- **Human in the loop.** LoveWorm never sends messages automatically. Every word you send is one you chose.
- **Privacy first.** Your conversations and data stay on your machine. Nothing is shared or used for training.
- **Respect platform rules.** LoveWorm does not automate swiping or scrape dating apps — that violates most apps' terms and can get your account banned.
- **Be yourself.** The goal is to help you show who you are, not to pretend to be someone else.

## Quickstart

```bash
git clone https://github.com/<org>/loveworm.git
cd loveworm
pip install -r requirements.txt
cp .env.example .env   # add your API key
python -m loveworm
```

## Configuration

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | API key for the LLM provider |

## Roadmap

- [ ] Profile review
- [ ] Conversation coach
- [ ] Date planner
- [ ] Web UI

## Contributing

Contributions are welcome! Open an issue to discuss what you'd like to change, or send a PR. See [CONTRIBUTING.md](https://github.com/<org>/.github/blob/main/CONTRIBUTING.md).

## License

[Apache-2.0](LICENSE)
