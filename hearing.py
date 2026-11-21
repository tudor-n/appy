def listen() -> str | None:
    try:
        return input("You: ").strip()
    except (KeyboardInterrupt, EOFError):
        return None